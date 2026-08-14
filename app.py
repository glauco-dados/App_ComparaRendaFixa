from __future__ import annotations

import json
import urllib.request
from datetime import date, timedelta
from io import BytesIO
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from bizdays import Calendar
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

st.set_page_config(page_title="Comparador LCI x CDB x Tesouro", layout="wide")

# =========================================================
# Constantes financeiras
# =========================================================
DIAS_UTEIS_ANO = 252
TAXA_CUSTODIA_TESOURO_AA_PADRAO = 0.002  # 0,20% a.a.
CALENDARIO_PADRAO = "ANBIMA"

CODIGO_SGS_SELIC_META = 432  # Meta Selic definida pelo Copom (% a.a.)
CODIGO_SGS_CDI_ANUALIZADO = 4389  # CDI anualizado, base 252 (% a.a.)

IR_REGRESSIVO = [
    (180, 0.225),
    (360, 0.200),
    (720, 0.175),
    (10_000_000, 0.150),
]


# =========================================================
# Utilidades de formatação
# =========================================================
def pct_to_decimal(x: float) -> float:
    return float(x) / 100


def decimal_to_pct(x: float) -> float:
    return float(x) * 100


def formata_moeda(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formata_pct(v: float) -> str:
    return f"{v * 100:,.2f}%".replace(",", "X").replace(".", ",").replace("X", ".")


def parse_moeda_brl(texto: str) -> float:
    limpo = texto.replace("R$", "").strip().replace(".", "").replace(",", ".")
    try:
        return float(limpo) if limpo else 0.0
    except ValueError:
        return 0.0


# =========================================================
# Calendário financeiro com bizdays
# =========================================================
@st.cache_resource(show_spinner=False)
def carrega_calendario(nome_calendario: str) -> Calendar:
    return Calendar.load(nome_calendario)


def data_iso(d: date) -> str:
    return d.isoformat()


def dias_uteis_bizdays(data_inicio: date, data_fim: date, nome_calendario: str) -> int:
    if data_fim <= data_inicio:
        return 0
    cal = carrega_calendario(nome_calendario)
    return int(cal.bizdays(data_iso(data_inicio), data_iso(data_fim)))


def dias_corridos(data_inicio: date, data_fim: date) -> int:
    return max((data_fim - data_inicio).days, 0)


# =========================================================
# Integração com o Banco Central (séries SGS)
# =========================================================
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def busca_taxa_sgs_bcb(codigo_serie: int) -> float | None:
    """Busca o valor mais recente de uma série do SGS/BCB, em decimal (ex.: 0.14 para 14%)."""
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo_serie}/dados/ultimos/1?formato=json"
    try:
        with urllib.request.urlopen(url, timeout=5) as resposta:
            dados = json.loads(resposta.read().decode("utf-8"))
        return pct_to_decimal(float(dados[-1]["valor"].replace(",", ".")))
    except Exception:
        return None


def busca_selic_meta_aa() -> float | None:
    return busca_taxa_sgs_bcb(CODIGO_SGS_SELIC_META)


def busca_cdi_aa() -> float | None:
    cdi = busca_taxa_sgs_bcb(CODIGO_SGS_CDI_ANUALIZADO)
    if cdi is not None:
        return cdi
    selic = busca_selic_meta_aa()
    if selic is not None:
        return max(selic - pct_to_decimal(0.10), 0.0)
    return None


# =========================================================
# Regras de cálculo
# =========================================================
def aliquota_ir(dias_dc: int) -> float:
    for limite, aliquota in IR_REGRESSIVO:
        if dias_dc <= limite:
            return aliquota
    return 0.15


def acumula_taxa(taxa_aa: float, dias_du: int) -> float:
    if dias_du <= 0:
        return 0.0
    return (1 + taxa_aa) ** (dias_du / DIAS_UTEIS_ANO) - 1


def taxa_anual_equivalente(retorno_periodo: float, dias_du: int) -> float:
    if dias_du <= 0:
        return 0.0
    return (1 + retorno_periodo) ** (DIAS_UTEIS_ANO / dias_du) - 1


def taxa_composta(taxa_a: float, taxa_b: float) -> float:
    """Compõe duas taxas anuais decimais, ex.: Selic + spread, ou Prefixado + IPCA."""
    return (1 + taxa_a) * (1 + taxa_b) - 1


def spread_sobre_indice(taxa_total_aa: float, indice_aa: float) -> float:
    """Taxa adicional decimal tal que (1 + indice) * (1 + spread) - 1 = taxa_total."""
    return (1 + taxa_total_aa) / (1 + indice_aa) - 1


def percentual_sobre_indice(taxa_total_aa: float, indice_aa: float) -> float:
    """Percentual decimal do índice equivalente a uma taxa total (ex.: 1.12 = 112% do CDI)."""
    if indice_aa == 0:
        return 0.0
    return taxa_total_aa / indice_aa


def calcula_custodia_tesouro(
    valor_inicial: float,
    taxa_aa: float,
    dias_du: int,
    taxa_custodia_aa: float,
    produto_tesouro: str,
    aplicar_isencao_selic_10k: bool,
) -> float:
    """Aproxima a custódia B3 por provisionamento diário pró-rata.

    A base é o valor do título projetado diariamente. Em caso de Tesouro Selic,
    o simulador permite considerar a isenção de custódia até R$ 10 mil por CPF,
    incidindo apenas sobre o excedente.
    """
    if dias_du <= 0 or taxa_custodia_aa <= 0 or valor_inicial <= 0:
        return 0.0

    taxa_dia = (1 + taxa_aa) ** (1 / DIAS_UTEIS_ANO) - 1
    saldo = valor_inicial
    custodia = 0.0

    for _ in range(dias_du):
        saldo *= 1 + taxa_dia
        base_custodia = saldo
        if produto_tesouro == "Tesouro Selic" and aplicar_isencao_selic_10k:
            base_custodia = max(saldo - 10_000, 0)
        custodia += base_custodia * (taxa_custodia_aa / DIAS_UTEIS_ANO)

    return custodia


def calcula_produto(
    tipo: str,
    rotulo: str,
    valor_inicial: float,
    taxa_aa: float,
    dias_du: int,
    dias_dc: int,
    taxa_custodia_aa: float,
    produto_tesouro: str,
    aplicar_isencao_selic_10k: bool,
) -> dict[str, Any]:
    tipo = tipo.upper().strip()
    retorno_bruto = acumula_taxa(taxa_aa, dias_du)
    valor_bruto = valor_inicial * (1 + retorno_bruto)
    rendimento_bruto = valor_bruto - valor_inicial

    ir = 0.0
    custodia = 0.0
    aliquota = 0.0

    if tipo in {"CDB", "TESOURO"}:
        aliquota = aliquota_ir(dias_dc)
        ir = rendimento_bruto * aliquota

    if tipo == "TESOURO":
        custodia = calcula_custodia_tesouro(
            valor_inicial=valor_inicial,
            taxa_aa=taxa_aa,
            dias_du=dias_du,
            taxa_custodia_aa=taxa_custodia_aa,
            produto_tesouro=produto_tesouro,
            aplicar_isencao_selic_10k=aplicar_isencao_selic_10k,
        )

    valor_liquido = valor_bruto - ir - custodia
    rendimento_liquido = valor_liquido - valor_inicial
    retorno_liquido_periodo = rendimento_liquido / valor_inicial if valor_inicial else 0.0
    taxa_liquida_aa = taxa_anual_equivalente(retorno_liquido_periodo, dias_du)

    return {
        "Produto": rotulo,
        "Taxa Bruta a.a.": taxa_aa,
        "Valor Bruto": valor_bruto,
        "Rendimento Bruto": rendimento_bruto,
        "IR": ir,
        "IR %": aliquota,
        "Custódia B3": custodia,
        "Valor Líquido": valor_liquido,
        "Rendimento Líquido": rendimento_liquido,
        "Taxa Líquida a.a.": taxa_liquida_aa,
    }


def taxa_bruta_por_taxa_liquida(
    tipo: str,
    taxa_liquida_aa: float,
    valor_inicial: float,
    dias_du: int,
    dias_dc: int,
    taxa_custodia_aa: float,
    produto_tesouro: str,
    aplicar_isencao_selic_10k: bool,
) -> float:
    tipo = tipo.upper().strip()

    if tipo == "LCI":
        return taxa_liquida_aa

    if tipo == "CDB":
        retorno_liq_periodo = acumula_taxa(taxa_liquida_aa, dias_du)
        aliquota = aliquota_ir(dias_dc)
        retorno_bruto_periodo = retorno_liq_periodo / (1 - aliquota)
        return taxa_anual_equivalente(retorno_bruto_periodo, dias_du)

    baixo, alto = -0.50, 2.00
    for _ in range(120):
        meio = (baixo + alto) / 2
        calc = calcula_produto(
            tipo="TESOURO",
            rotulo=produto_tesouro,
            valor_inicial=valor_inicial,
            taxa_aa=meio,
            dias_du=dias_du,
            dias_dc=dias_dc,
            taxa_custodia_aa=taxa_custodia_aa,
            produto_tesouro=produto_tesouro,
            aplicar_isencao_selic_10k=aplicar_isencao_selic_10k,
        )
        if calc["Taxa Líquida a.a."] < taxa_liquida_aa:
            baixo = meio
        else:
            alto = meio
    return (baixo + alto) / 2


def serie_temporal_valor_liquido(
    valor_inicial: float,
    taxa_lci: float,
    taxa_cdb: float,
    taxa_tesouro: float,
    rotulo_lci: str,
    rotulo_cdb: str,
    rotulo_tesouro: str,
    data_inicio: date,
    data_fim: date,
    calendario_nome: str,
    taxa_custodia_aa: float,
    produto_tesouro: str,
    aplicar_isencao_selic_10k: bool,
) -> pd.DataFrame:
    """Projeta o valor líquido de resgate dia a dia, para o gráfico de evolução no tempo."""
    cal = carrega_calendario(calendario_nome)
    taxa_dia_custodia = taxa_custodia_aa / DIAS_UTEIS_ANO

    taxa_dia = {
        rotulo_lci: (1 + taxa_lci) ** (1 / DIAS_UTEIS_ANO) - 1,
        rotulo_cdb: (1 + taxa_cdb) ** (1 / DIAS_UTEIS_ANO) - 1,
        rotulo_tesouro: (1 + taxa_tesouro) ** (1 / DIAS_UTEIS_ANO) - 1,
    }
    saldo = {nome: valor_inicial for nome in taxa_dia}
    custodia_acumulada = 0.0

    linhas: list[dict[str, Any]] = []
    d = data_inicio
    while d <= data_fim:
        if cal.isbizday(data_iso(d)):
            for nome in saldo:
                saldo[nome] *= 1 + taxa_dia[nome]

            base_custodia = saldo[rotulo_tesouro]
            if produto_tesouro == "Tesouro Selic" and aplicar_isencao_selic_10k:
                base_custodia = max(saldo[rotulo_tesouro] - 10_000, 0)
            custodia_acumulada += base_custodia * taxa_dia_custodia

            aliquota = aliquota_ir(dias_corridos(data_inicio, d))

            valor_liquido_lci = saldo[rotulo_lci]
            valor_liquido_cdb = saldo[rotulo_cdb] - max(saldo[rotulo_cdb] - valor_inicial, 0) * aliquota
            valor_liquido_tesouro = (
                saldo[rotulo_tesouro]
                - max(saldo[rotulo_tesouro] - valor_inicial, 0) * aliquota
                - custodia_acumulada
            )

            linhas.append({"Data": d, "Produto": rotulo_lci, "Valor Líquido": valor_liquido_lci})
            linhas.append({"Data": d, "Produto": rotulo_cdb, "Valor Líquido": valor_liquido_cdb})
            linhas.append({"Data": d, "Produto": rotulo_tesouro, "Valor Líquido": valor_liquido_tesouro})
        d += timedelta(days=1)

    return pd.DataFrame(linhas)


# =========================================================
# Geração de relatório em PDF
# =========================================================
def gerar_pdf_comparativo(tabela_exibicao: pd.DataFrame, resumo: dict[str, str]) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), title="Comparativo LCI x CDB x Tesouro")
    estilos = getSampleStyleSheet()

    elementos = [
        Paragraph("Comparador LCI x CDB x Tesouro Direto", estilos["Title"]),
        Paragraph(
            f"Valor inicial: {resumo['valor_inicial']} &nbsp;|&nbsp; "
            f"Aplicação: {resumo['data_inicio']} &nbsp;|&nbsp; "
            f"Vencimento: {resumo['data_fim']} &nbsp;|&nbsp; "
            f"Dias úteis: {resumo['du']} &nbsp;|&nbsp; "
            f"Dias corridos: {resumo['dc']} &nbsp;|&nbsp; "
            f"Alíquota IR: {resumo['aliquota']}",
            estilos["Normal"],
        ),
        Spacer(1, 14),
    ]

    dados_tabela = [list(tabela_exibicao.columns)] + tabela_exibicao.values.tolist()
    tabela = Table(dados_tabela, repeatRows=1)
    tabela.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ]
        )
    )
    elementos.append(tabela)
    elementos.append(Spacer(1, 14))
    elementos.append(Paragraph(resumo["melhor"], estilos["Normal"]))

    doc.build(elementos)
    return buffer.getvalue()


# =========================================================
# Entradas de taxa por indexador (widgets)
# =========================================================
def input_taxa_bruta_lci_cdb(
    coluna,
    produto: str,
    indexador: str,
    key_prefix: str,
    default_pre: float,
    default_pct_cdi: float,
    default_ipca_pre: float,
) -> float:
    if indexador == "CDI":
        cdi_aa = busca_cdi_aa()
        if cdi_aa is None:
            coluna.caption("CDI não encontrado no Banco Central; informe manualmente.")
            cdi_aa = pct_to_decimal(
                coluna.number_input(
                    f"{produto} - CDI a.a. (%)",
                    value=13.90,
                    step=0.05,
                    format="%.4f",
                    key=f"{key_prefix}_cdi_manual",
                )
            )
        else:
            coluna.caption(f"CDI vigente (Banco Central): {formata_pct(cdi_aa)} a.a.")
        percentual_cdi = coluna.number_input(
            f"{produto} - % do CDI",
            value=default_pct_cdi,
            step=1.00,
            format="%.2f",
            key=f"{key_prefix}_pct_cdi",
        )
        return cdi_aa * pct_to_decimal(percentual_cdi)

    if indexador == "IPCA+":
        prefixada = coluna.number_input(
            f"{produto} - taxa prefixada (%)",
            value=default_ipca_pre,
            step=0.10,
            format="%.4f",
            key=f"{key_prefix}_ipca_pre",
        )
        ipca_estimado = coluna.number_input(
            f"{produto} - IPCA estimado (%)",
            value=4.50,
            step=0.10,
            format="%.4f",
            key=f"{key_prefix}_ipca_est",
        )
        return taxa_composta(pct_to_decimal(prefixada), pct_to_decimal(ipca_estimado))

    taxa = coluna.number_input(
        f"{produto} - taxa bruta a.a. (%)",
        value=default_pre,
        step=0.10,
        format="%.4f",
        key=f"{key_prefix}_pre",
    )
    return pct_to_decimal(taxa)


def input_taxa_bruta_tesouro(coluna, produto_tesouro: str) -> float:
    if produto_tesouro == "Tesouro Selic":
        selic_aa = busca_selic_meta_aa()
        if selic_aa is None:
            coluna.caption("Selic não encontrada no Banco Central; valor padrão sugerido.")
            selic_default_pct = 14.00
        else:
            coluna.caption(f"Meta Selic vigente (Banco Central): {formata_pct(selic_aa)} a.a.")
            selic_default_pct = round(decimal_to_pct(selic_aa), 4)
        selic_informada = coluna.number_input(
            "Tesouro Selic - taxa Selic (%)",
            value=selic_default_pct,
            step=0.05,
            format="%.4f",
            key="tesouro_selic_taxa",
        )
        spread = coluna.number_input(
            "Tesouro Selic - taxa adicional (%)",
            value=0.0739,
            step=0.0001,
            format="%.4f",
            key="tesouro_selic_spread",
        )
        return taxa_composta(pct_to_decimal(selic_informada), pct_to_decimal(spread))

    if produto_tesouro == "Tesouro IPCA+":
        prefixada = coluna.number_input(
            "Tesouro IPCA+ - taxa prefixada (%)",
            value=6.00,
            step=0.10,
            format="%.4f",
            key="tesouro_ipca_pre",
        )
        ipca_estimado = coluna.number_input(
            "Tesouro IPCA+ - IPCA estimado (%)",
            value=4.50,
            step=0.10,
            format="%.4f",
            key="tesouro_ipca_est",
        )
        return taxa_composta(pct_to_decimal(prefixada), pct_to_decimal(ipca_estimado))

    taxa = coluna.number_input(
        "Tesouro Prefixado - taxa bruta a.a. (%)",
        value=14.00,
        step=0.10,
        format="%.4f",
        key="tesouro_pre",
    )
    return pct_to_decimal(taxa)


# =========================================================
# Interface Streamlit
# =========================================================
st.title("Comparador LCI x CDB x Tesouro Direto")
st.caption(
    "Comparação de rentabilidade líquida com calendário financeiro ANBIMA, "
    "IR regressivo e taxa de custódia do Tesouro Direto."
)

with st.sidebar:
    st.subheader("Parâmetros gerais")

    if "valor_inicial_input" not in st.session_state:
        st.session_state.valor_inicial_input = formata_moeda(100000.0)

    def _normaliza_valor_inicial() -> None:
        valor = parse_moeda_brl(st.session_state.valor_inicial_input)
        st.session_state.valor_inicial_input = formata_moeda(valor)

    st.text_input(
        "Valor inicial",
        key="valor_inicial_input",
        on_change=_normaliza_valor_inicial,
        help="Formato: R$ 0.000,00",
    )
    valor_inicial = parse_moeda_brl(st.session_state.valor_inicial_input)

    data_inicio = st.date_input("Data da aplicação", value=date.today(), format="DD/MM/YYYY")

    modo_vencimento = st.radio(
        "Definir vencimento por",
        ["Data de vencimento", "Dias corridos"],
        index=0,
    )
    if modo_vencimento == "Data de vencimento":
        data_fim = st.date_input(
            "Data de vencimento/resgate",
            value=date.today() + timedelta(days=366),
            format="DD/MM/YYYY",
        )
    else:
        dias_corridos_informados = st.number_input(
            "Dias corridos a partir da aplicação",
            min_value=1,
            value=366,
            step=1,
        )
        data_fim = data_inicio + timedelta(days=int(dias_corridos_informados))

    aplicar_isencao_selic_10k = st.checkbox(
        "Aplicar isenção de custódia até R$ 10 mil para Tesouro Selic",
        value=True,
        help="A regra é aplicável ao Tesouro Selic. Para Prefixado e IPCA+, o checkbox não altera a base.",
    )
    taxa_custodia_aa = pct_to_decimal(
        st.number_input("Taxa de custódia Tesouro a.a. (%)", value=0.20, step=0.01, format="%.4f")
    )

    modo = st.radio("Modo de comparação", ["Informar taxa bruta", "Informar taxa líquida desejada"], index=0)

calendario_nome = CALENDARIO_PADRAO

try:
    DU = dias_uteis_bizdays(data_inicio, data_fim, calendario_nome)
except Exception as exc:
    st.error(f"Não foi possível carregar o calendário '{calendario_nome}' pelo bizdays. Detalhe: {exc}")
    st.stop()

DC = dias_corridos(data_inicio, data_fim)
ALIQUOTA = aliquota_ir(DC)

k1, k2, k3, k4 = st.columns(4)
k1.metric("Dias corridos", DC)
k2.metric("Dias úteis", DU)
k3.metric("Alíquota IR", formata_pct(ALIQUOTA))
k4.metric("Calendário", calendario_nome)

if DU <= 0:
    st.warning("Informe uma data final posterior à data inicial.")
    st.stop()

st.subheader("Taxas dos produtos")

col1, col2, col3 = st.columns(3)
indexador_lci = col1.selectbox("Indexador LCI", ["CDI", "Prefixado", "IPCA+"], index=0)
indexador_cdb = col2.selectbox("Indexador CDB", ["CDI", "Prefixado", "IPCA+"], index=0)
produto_tesouro = col3.selectbox(
    "Indexador Tesouro",
    ["Tesouro Prefixado", "Tesouro IPCA+", "Tesouro Selic"],
    index=0,
)

rotulo_lci = f"LCI ({indexador_lci})"
rotulo_cdb = f"CDB ({indexador_cdb})"
rotulo_tesouro = produto_tesouro

if modo == "Informar taxa bruta":
    taxa_lci = input_taxa_bruta_lci_cdb(
        col1, "LCI", indexador_lci, "lci",
        default_pre=12.00, default_pct_cdi=90.00, default_ipca_pre=5.50,
    )
    taxa_cdb = input_taxa_bruta_lci_cdb(
        col2, "CDB", indexador_cdb, "cdb",
        default_pre=14.00, default_pct_cdi=100.00, default_ipca_pre=6.00,
    )
    taxa_tesouro = input_taxa_bruta_tesouro(col3, produto_tesouro)
else:
    taxa_lci_liq = pct_to_decimal(col1.number_input("LCI - taxa líquida a.a. (%)", value=12.00, step=0.10, format="%.4f"))
    taxa_cdb_liq = pct_to_decimal(col2.number_input("CDB - taxa líquida a.a. (%)", value=12.00, step=0.10, format="%.4f"))
    taxa_tesouro_liq = pct_to_decimal(col3.number_input(f"{produto_tesouro} - taxa líquida a.a. (%)", value=12.00, step=0.10, format="%.4f"))

    taxa_lci = taxa_bruta_por_taxa_liquida("LCI", taxa_lci_liq, valor_inicial, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k)
    taxa_cdb = taxa_bruta_por_taxa_liquida("CDB", taxa_cdb_liq, valor_inicial, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k)
    taxa_tesouro = taxa_bruta_por_taxa_liquida("TESOURO", taxa_tesouro_liq, valor_inicial, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k)

resultados = [
    calcula_produto("LCI", rotulo_lci, valor_inicial, taxa_lci, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k),
    calcula_produto("CDB", rotulo_cdb, valor_inicial, taxa_cdb, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k),
    calcula_produto("TESOURO", rotulo_tesouro, valor_inicial, taxa_tesouro, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k),
]

df = pd.DataFrame(resultados).sort_values("Valor Líquido", ascending=False).reset_index(drop=True)

st.subheader("Resultado comparativo")

styled = df.copy()
for c in ["Taxa Bruta a.a.", "Taxa Líquida a.a.", "IR %"]:
    styled[c] = styled[c].map(formata_pct)
for c in ["Valor Bruto", "Rendimento Bruto", "IR", "Custódia B3", "Valor Líquido", "Rendimento Líquido"]:
    styled[c] = styled[c].map(formata_moeda)

st.dataframe(styled, use_container_width=True, hide_index=True)

melhor = df.iloc[0]
st.success(
    f"Melhor alternativa pelo valor líquido: {melhor['Produto']} com "
    f"{formata_moeda(melhor['Valor Líquido'])} e taxa líquida de "
    f"{formata_pct(melhor['Taxa Líquida a.a.'])} a.a."
)

st.subheader("Gráficos")

serie = serie_temporal_valor_liquido(
    valor_inicial=valor_inicial,
    taxa_lci=taxa_lci,
    taxa_cdb=taxa_cdb,
    taxa_tesouro=taxa_tesouro,
    rotulo_lci=rotulo_lci,
    rotulo_cdb=rotulo_cdb,
    rotulo_tesouro=rotulo_tesouro,
    data_inicio=data_inicio,
    data_fim=data_fim,
    calendario_nome=calendario_nome,
    taxa_custodia_aa=taxa_custodia_aa,
    produto_tesouro=produto_tesouro,
    aplicar_isencao_selic_10k=aplicar_isencao_selic_10k,
)

MESES_PT_ABREV = "['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']"

grafico_valor_liquido = (
    alt.Chart(serie)
    .mark_line(
        interpolate="monotone",
        strokeWidth=1.6,
        opacity=0.9,
        point=alt.OverlayMarkDef(size=16, filled=True, opacity=0.55),
    )
    .encode(
        x=alt.X(
            "Data:T",
            title="Data",
            axis=alt.Axis(labelExpr=f"{MESES_PT_ABREV}[month(datum.value)] + ' ' + year(datum.value)"),
        ),
        y=alt.Y(
            "Valor Líquido:Q",
            title="Valor Líquido (R$)",
            axis=alt.Axis(format=",.2f"),
            scale=alt.Scale(domainMin=valor_inicial, nice=False),
        ),
        color=alt.Color("Produto:N", title="Produto"),
        tooltip=[
            alt.Tooltip("Data:T", title="Data", format="%d/%m/%Y"),
            alt.Tooltip("Produto:N", title="Produto"),
            alt.Tooltip("Valor Líquido:Q", title="Valor Líquido", format=",.2f"),
        ],
    )
    .properties(height=420)
)
st.altair_chart(grafico_valor_liquido, use_container_width=True)

st.subheader("Equivalência em relação à LCI")
base_lci = df[df["Produto"] == rotulo_lci].iloc[0]

# A comparação do Tesouro segue o indexador escolhido para a LCI, independentemente
# do indexador selecionado na seção "Taxas dos produtos" para o Tesouro.
tipo_tesouro_ref = produto_tesouro
if modo == "Informar taxa bruta":
    if indexador_lci == "CDI":
        tipo_tesouro_ref = "Tesouro Selic"
    elif indexador_lci == "IPCA+":
        tipo_tesouro_ref = "Tesouro IPCA+"

cdb_equiv = taxa_bruta_por_taxa_liquida("CDB", base_lci["Taxa Líquida a.a."], valor_inicial, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k)
tesouro_equiv = taxa_bruta_por_taxa_liquida("TESOURO", base_lci["Taxa Líquida a.a."], valor_inicial, DU, DC, taxa_custodia_aa, tipo_tesouro_ref, aplicar_isencao_selic_10k)

rotulo_cdb_equiv = "CDB bruto equivalente à LCI"
valor_cdb_equiv = formata_pct(cdb_equiv)
rotulo_tesouro_equiv = f"{produto_tesouro} bruto equivalente à LCI"
valor_tesouro_equiv = formata_pct(tesouro_equiv)

if modo == "Informar taxa bruta" and indexador_lci == "CDI":
    cdi_aa_ref = busca_cdi_aa()
    if cdi_aa_ref is None and "lci_cdi_manual" in st.session_state:
        cdi_aa_ref = pct_to_decimal(st.session_state["lci_cdi_manual"])
    if cdi_aa_ref:
        rotulo_cdb_equiv = "CDB equivalente à LCI"
        valor_cdb_equiv = f"{formata_pct(percentual_sobre_indice(cdb_equiv, cdi_aa_ref))} do CDI"

    selic_ref = busca_selic_meta_aa()
    if selic_ref is None and "tesouro_selic_taxa" in st.session_state:
        selic_ref = pct_to_decimal(st.session_state["tesouro_selic_taxa"])
    if selic_ref:
        rotulo_tesouro_equiv = "Tesouro Selic equivalente à LCI"
        valor_tesouro_equiv = f"Selic + {formata_pct(spread_sobre_indice(tesouro_equiv, selic_ref))}"
elif modo == "Informar taxa bruta" and indexador_lci == "IPCA+" and "lci_ipca_est" in st.session_state:
    ipca_ref = pct_to_decimal(st.session_state["lci_ipca_est"])
    rotulo_cdb_equiv = "CDB equivalente à LCI"
    valor_cdb_equiv = f"IPCA + {formata_pct(spread_sobre_indice(cdb_equiv, ipca_ref))}"
    rotulo_tesouro_equiv = "Tesouro IPCA+ equivalente à LCI"
    valor_tesouro_equiv = f"IPCA + {formata_pct(spread_sobre_indice(tesouro_equiv, ipca_ref))}"

col_a, col_b = st.columns(2)
col_a.metric(rotulo_cdb_equiv, valor_cdb_equiv)
col_b.metric(rotulo_tesouro_equiv, valor_tesouro_equiv)

with st.expander("Detalhes metodológicos"):
    st.markdown(
        """
        - **Calendário:** cálculo de dias úteis via `bizdays`, sempre pelo calendário ANBIMA.
        - **Base anual:** 252 dias úteis.
        - **Vencimento:** informado por data ou por quantidade de dias corridos a partir da aplicação.
        - **LCI:** considerada isenta de IR para pessoa física.
        - **CDB:** IR regressivo sobre o rendimento bruto.
        - **Tesouro Direto:** IR regressivo sobre o rendimento bruto e taxa de custódia B3 pró-rata diária.
        - **Tesouro Selic:** opção para aplicar isenção de custódia até R$ 10 mil por CPF.
        - **Indexador CDI:** percentual informado aplicado sobre o CDI anualizado (base 252), buscado
          automaticamente no Banco Central (série SGS 4389), com contingência para Meta Selic - 0,10 p.p.
        - **Indexador Selic (Tesouro):** Meta Selic (série SGS 432, buscada automaticamente) composta com a
          taxa adicional (ágio/deságio) informada.
        - **Indexador IPCA+:** taxa prefixada composta com o IPCA estimado informado pelo usuário.
        - **Limitação:** para Tesouro Prefixado/IPCA+ vendido antes do vencimento, o app não modela marcação a mercado.
        """
    )

csv = df.to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig")

pdf_bytes = gerar_pdf_comparativo(
    styled,
    {
        "valor_inicial": formata_moeda(valor_inicial),
        "data_inicio": f"{data_inicio:%d/%m/%Y}",
        "data_fim": f"{data_fim:%d/%m/%Y}",
        "du": str(DU),
        "dc": str(DC),
        "aliquota": formata_pct(ALIQUOTA),
        "melhor": (
            f"Melhor alternativa pelo valor líquido: {melhor['Produto']} com "
            f"{formata_moeda(melhor['Valor Líquido'])} e taxa líquida de "
            f"{formata_pct(melhor['Taxa Líquida a.a.'])} a.a."
        ),
    },
)

col_csv, col_pdf = st.columns(2)
with col_csv:
    st.download_button(
        "Baixar resultado em CSV",
        data=csv.encode("utf-8-sig"),
        file_name="comparativo_lci_cdb_tesouro.csv",
        mime="text/csv",
    )
with col_pdf:
    st.download_button(
        "Baixar resultado em PDF",
        data=pdf_bytes,
        file_name="comparativo_lci_cdb_tesouro.pdf",
        mime="application/pdf",
    )

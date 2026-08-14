from __future__ import annotations

import json
import urllib.request
from datetime import date, timedelta
from io import BytesIO
from typing import Any

import pandas as pd
import plotly.graph_objects as go
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

# Calendário oficial de reuniões do Copom (data da decisão = segundo dia da reunião),
# conforme divulgado pelo Banco Central. Cobre o horizonte conhecido no momento; períodos
# além da última data listada são estimados por ciclo de 45 dias (ver calendario_copom_no_periodo).
CALENDARIO_COPOM_OFICIAL: list[date] = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 8, 5), date(2026, 9, 16), date(2026, 11, 4), date(2026, 12, 9),
    date(2027, 1, 27), date(2027, 3, 17), date(2027, 4, 28), date(2027, 6, 16),
    date(2027, 8, 4), date(2027, 9, 22), date(2027, 10, 27), date(2027, 12, 8),
]
CICLO_COPOM_DIAS = 45


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


def calendario_copom_no_periodo(data_inicio: date, data_fim: date) -> list[tuple[date, bool]]:
    """Retorna [(data_da_decisao, é_data_oficial)] para reuniões entre data_inicio (exclusive) e data_fim.

    Usa o calendário oficial conhecido; se o período ultrapassar a última data oficial,
    estima reuniões adicionais a cada CICLO_COPOM_DIAS dias.
    """
    reunioes = [(d, True) for d in CALENDARIO_COPOM_OFICIAL if data_inicio < d <= data_fim]

    ultima_conhecida = CALENDARIO_COPOM_OFICIAL[-1]
    if data_fim > ultima_conhecida:
        proxima = ultima_conhecida + timedelta(days=CICLO_COPOM_DIAS)
        while proxima <= data_fim:
            if proxima > data_inicio:
                reunioes.append((proxima, False))
            proxima += timedelta(days=CICLO_COPOM_DIAS)

    return sorted(reunioes, key=lambda item: item[0])


def constroi_trajetoria(nivel_inicial: float, ajustes: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """ajustes: [(data_da_reuniao, delta_decimal)] ordenado por data.

    Retorna um vetor de vigência [(data_a_partir_da_qual_vale, nivel_aa_decimal)], onde o
    novo nível passa a valer no dia seguinte à decisão do Copom.
    """
    trajetoria = [(date.min, nivel_inicial)]
    nivel = nivel_inicial
    for data_reuniao, delta in ajustes:
        nivel += delta
        trajetoria.append((data_reuniao + timedelta(days=1), nivel))
    return trajetoria


def nivel_na_data(trajetoria: list[tuple[date, float]], d: date) -> float:
    nivel_atual = trajetoria[0][1]
    for data_vigencia, nivel in trajetoria:
        if data_vigencia <= d:
            nivel_atual = nivel
        else:
            break
    return nivel_atual


def retorno_termo(
    data_inicio: date,
    data_fim: date,
    calendario_nome: str,
    trajetoria: list[tuple[date, float]],
) -> float:
    """Compõe o retorno do período percorrendo dia útil a dia útil, aplicando o nível vigente em cada data."""
    cal = carrega_calendario(calendario_nome)
    fator = 1.0
    d = data_inicio
    while d <= data_fim:
        if cal.isbizday(data_iso(d)):
            nivel = nivel_na_data(trajetoria, d)
            fator *= (1 + nivel) ** (1 / DIAS_UTEIS_ANO)
        d += timedelta(days=1)
    return fator - 1


def constroi_trajetoria_cdi(trajetoria_selic: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Deriva a trajetória do CDI a partir da trajetória da Selic, mantendo o spread atual entre elas."""
    cdi_atual = busca_cdi_aa()
    selic_atual = trajetoria_selic[0][1]
    spread_cdi_selic = (cdi_atual - selic_atual) if cdi_atual is not None else -pct_to_decimal(0.10)
    return [(d, nivel + spread_cdi_selic) for d, nivel in trajetoria_selic]


def rotulo_automatico(tipo: str, indexador_ou_subtipo: str) -> str:
    if tipo == "TESOURO":
        return indexador_ou_subtipo
    return f"{tipo} ({indexador_ou_subtipo})"


def _subtipo_key_padrao(key_prefix: str, tipo: str) -> str:
    return f"{key_prefix}_subtipo_tesouro" if tipo == "TESOURO" else f"{key_prefix}_subtipo_lci_cdb"


def _subtipo_default(tipo: str) -> str:
    return "Tesouro Prefixado" if tipo == "TESOURO" else "CDI"


def _atualiza_rotulo_automatico(key_prefix: str) -> None:
    tipo = st.session_state[f"{key_prefix}_tipo"]
    subtipo_key = _subtipo_key_padrao(key_prefix, tipo)
    subtipo = st.session_state.get(subtipo_key, _subtipo_default(tipo))
    st.session_state[f"{key_prefix}_rotulo"] = rotulo_automatico(tipo, subtipo)


def rotulos_unicos(rotulos: list[str]) -> list[str]:
    """Desambigua rótulos repetidos (ex.: duas ofertas de CDB) anexando '(2)', '(3)' etc."""
    contagem: dict[str, int] = {}
    resultado = []
    for r in rotulos:
        contagem[r] = contagem.get(r, 0) + 1
        resultado.append(r if contagem[r] == 1 else f"{r} ({contagem[r]})")
    return resultado


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
    produtos_calc: list[dict[str, Any]],
    data_inicio: date,
    data_fim: date,
    calendario_nome: str,
    taxa_custodia_aa: float,
    aplicar_isencao_selic_10k: bool,
) -> pd.DataFrame:
    """Projeta o valor líquido de resgate dia a dia, para o gráfico de evolução no tempo.

    `produtos_calc` é uma lista de dicts com as chaves: rotulo, tipo, taxa_aa e,
    para tipo TESOURO, subtipo (ex.: "Tesouro Selic"). Quando um produto tem a chave
    opcional "trajetoria" (vetor de vigência no formato de `constroi_trajetoria`), o
    fator diário é obtido do nível vigente naquela data em vez da taxa_aa fixa.
    """
    cal = carrega_calendario(calendario_nome)
    taxa_dia_custodia = taxa_custodia_aa / DIAS_UTEIS_ANO

    taxa_dia_fixa = {
        p["rotulo"]: (1 + p["taxa_aa"]) ** (1 / DIAS_UTEIS_ANO) - 1
        for p in produtos_calc
        if not p.get("trajetoria")
    }
    saldo = {p["rotulo"]: valor_inicial for p in produtos_calc}
    custodia_acumulada = {p["rotulo"]: 0.0 for p in produtos_calc if p["tipo"] == "TESOURO"}

    linhas: list[dict[str, Any]] = []
    d = data_inicio
    while d <= data_fim:
        if cal.isbizday(data_iso(d)):
            aliquota = aliquota_ir(dias_corridos(data_inicio, d))
            for p in produtos_calc:
                rotulo = p["rotulo"]
                trajetoria = p.get("trajetoria")
                if trajetoria:
                    fator_dia = (1 + nivel_na_data(trajetoria, d)) ** (1 / DIAS_UTEIS_ANO)
                else:
                    fator_dia = 1 + taxa_dia_fixa[rotulo]
                saldo[rotulo] *= fator_dia

                if p["tipo"] == "TESOURO":
                    base_custodia = saldo[rotulo]
                    if p.get("subtipo") == "Tesouro Selic" and aplicar_isencao_selic_10k:
                        base_custodia = max(saldo[rotulo] - 10_000, 0)
                    custodia_acumulada[rotulo] += base_custodia * taxa_dia_custodia

                if p["tipo"] == "LCI":
                    valor_liquido = saldo[rotulo]
                elif p["tipo"] == "CDB":
                    valor_liquido = saldo[rotulo] - max(saldo[rotulo] - valor_inicial, 0) * aliquota
                else:  # TESOURO
                    valor_liquido = (
                        saldo[rotulo]
                        - max(saldo[rotulo] - valor_inicial, 0) * aliquota
                        - custodia_acumulada[rotulo]
                    )

                linhas.append({"Data": d, "Produto": rotulo, "Valor Líquido": valor_liquido})
        d += timedelta(days=1)

    return pd.DataFrame(linhas)


MESES_PT_ABREV = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def gera_ticks_meses_pt(data_inicio: date, data_fim: date) -> tuple[list[pd.Timestamp], list[str]]:
    """Gera as posições e os rótulos (em português) das marcações mensais do eixo X."""
    inicio_mes = pd.Timestamp(data_inicio).replace(day=1)
    ticks = pd.date_range(inicio_mes, data_fim, freq="MS")
    ticktext = [f"{MESES_PT_ABREV[d.month - 1]}/{d.year}" for d in ticks]
    return list(ticks), ticktext


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


def input_taxa_bruta_tesouro(coluna, produto_tesouro: str, key_prefix: str) -> float:
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
            key=f"{key_prefix}_tesouro_selic_taxa",
        )
        spread = coluna.number_input(
            "Tesouro Selic - taxa adicional (%)",
            value=0.0739,
            step=0.0001,
            format="%.4f",
            key=f"{key_prefix}_tesouro_selic_spread",
        )
        return taxa_composta(pct_to_decimal(selic_informada), pct_to_decimal(spread))

    if produto_tesouro == "Tesouro IPCA+":
        prefixada = coluna.number_input(
            "Tesouro IPCA+ - taxa prefixada (%)",
            value=6.00,
            step=0.10,
            format="%.4f",
            key=f"{key_prefix}_tesouro_ipca_pre",
        )
        ipca_estimado = coluna.number_input(
            "Tesouro IPCA+ - IPCA estimado (%)",
            value=4.50,
            step=0.10,
            format="%.4f",
            key=f"{key_prefix}_tesouro_ipca_est",
        )
        return taxa_composta(pct_to_decimal(prefixada), pct_to_decimal(ipca_estimado))

    taxa = coluna.number_input(
        "Tesouro Prefixado - taxa bruta a.a. (%)",
        value=14.00,
        step=0.10,
        format="%.4f",
        key=f"{key_prefix}_tesouro_pre",
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

    st.divider()
    st.subheader("Parâmetro avançado: trajetória Selic (Copom)")
    usar_trajetoria_copom = st.checkbox(
        "Estimar decisões do Copom reunião a reunião",
        value=False,
        help=(
            "Substitui a Selic/CDI constante por uma trajetória construída reunião a reunião. "
            "Aplica-se apenas no modo 'Informar taxa bruta', a produtos indexados a CDI e ao "
            "Tesouro Selic."
        ),
    )

    trajetoria_selic: list[tuple[date, float]] | None = None
    reunioes_no_periodo: list[tuple[date, bool]] = []

    if usar_trajetoria_copom:
        reunioes_no_periodo = calendario_copom_no_periodo(data_inicio, data_fim)

        if modo != "Informar taxa bruta":
            st.caption("A trajetória só é aplicada no modo 'Informar taxa bruta'.")
        elif not reunioes_no_periodo:
            st.caption("Nenhuma reunião do Copom prevista no período informado.")
        else:
            selic_atual = busca_selic_meta_aa()
            if selic_atual is None:
                selic_atual = pct_to_decimal(
                    st.number_input(
                        "Selic vigente (%)", value=14.00, step=0.05, format="%.2f", key="copom_selic_base"
                    )
                )
            else:
                st.caption(f"Selic vigente (Banco Central): {formata_pct(selic_atual)} a.a.")

            st.caption(
                "Datas sem indicação '(estimada)' vêm do calendário oficial divulgado pelo BC; "
                "as demais são estimadas por ciclo de 45 dias. O CDI é projetado acompanhando "
                "integralmente as variações da Selic, mantendo o spread atual entre os dois."
            )

            if st.button("Zerar cenário de reuniões"):
                for data_reuniao, _ in reunioes_no_periodo:
                    st.session_state.pop(f"copom_delta_{data_reuniao.isoformat()}", None)
                st.rerun()

            ajustes: list[tuple[date, float]] = []
            nivel_acumulado = selic_atual
            for data_reuniao, oficial in reunioes_no_periodo:
                sufixo = "" if oficial else " (estimada)"
                delta_pct = st.number_input(
                    f"{data_reuniao:%d/%m/%Y}{sufixo} — variação (p.p.)",
                    value=0.00,
                    step=0.25,
                    format="%.2f",
                    key=f"copom_delta_{data_reuniao.isoformat()}",
                )
                delta = pct_to_decimal(delta_pct)
                nivel_acumulado += delta
                ajustes.append((data_reuniao, delta))
                st.caption(f"Selic projetada após a reunião: {formata_pct(nivel_acumulado)} a.a.")

            trajetoria_selic = constroi_trajetoria(selic_atual, ajustes)

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

trajetoria_cdi_global = constroi_trajetoria_cdi(trajetoria_selic) if trajetoria_selic else None

st.subheader("Produtos comparados")

if "produtos" not in st.session_state:
    st.session_state.produtos = [
        {"id": 1, "tipo": "LCI"},
        {"id": 2, "tipo": "CDB"},
        {"id": 3, "tipo": "TESOURO"},
    ]
    st.session_state.proximo_id_produto = 4

if st.button("+ Adicionar produto"):
    novo_id = st.session_state.proximo_id_produto
    st.session_state.produtos.append({"id": novo_id, "tipo": "CDB"})
    st.session_state.proximo_id_produto = novo_id + 1
    st.rerun()

cards: list[dict[str, Any]] = []
for produto in st.session_state.produtos:
    pid = produto["id"]
    key_prefix = f"p{pid}"

    with st.container(border=True):
        c_tipo, c_rotulo, c_del = st.columns([1, 2.5, 0.7])
        tipo = c_tipo.selectbox(
            "Tipo",
            ["LCI", "CDB", "TESOURO"],
            index=["LCI", "CDB", "TESOURO"].index(produto["tipo"]),
            key=f"{key_prefix}_tipo",
            on_change=_atualiza_rotulo_automatico,
            args=(key_prefix,),
        )
        produto["tipo"] = tipo

        subtipo_key = _subtipo_key_padrao(key_prefix, tipo)
        if tipo == "TESOURO":
            subtipo = c_tipo.selectbox(
                "Título",
                ["Tesouro Prefixado", "Tesouro IPCA+", "Tesouro Selic"],
                key=subtipo_key,
                on_change=_atualiza_rotulo_automatico,
                args=(key_prefix,),
            )
        else:
            subtipo = c_tipo.selectbox(
                "Indexador",
                ["CDI", "Prefixado", "IPCA+"],
                key=subtipo_key,
                on_change=_atualiza_rotulo_automatico,
                args=(key_prefix,),
            )

        rotulo_key = f"{key_prefix}_rotulo"
        if rotulo_key not in st.session_state:
            st.session_state[rotulo_key] = rotulo_automatico(tipo, subtipo)
        rotulo = c_rotulo.text_input(
            "Rótulo (identificação da oferta)",
            key=rotulo_key,
            help="O rótulo é atualizado automaticamente ao trocar tipo ou indexador; edite livremente para personalizar.",
        )

        c_del.markdown("&nbsp;")
        pode_remover = len(st.session_state.produtos) > 1
        if c_del.button("Remover", key=f"{key_prefix}_remove", disabled=not pode_remover):
            st.session_state.produtos = [p for p in st.session_state.produtos if p["id"] != pid]
            st.rerun()

        card_trajetoria = None

        if modo == "Informar taxa bruta":
            if tipo == "TESOURO":
                taxa_aa = input_taxa_bruta_tesouro(st, subtipo, key_prefix)
            else:
                taxa_aa = input_taxa_bruta_lci_cdb(
                    st, tipo, subtipo, key_prefix,
                    default_pre=12.00 if tipo == "LCI" else 14.00,
                    default_pct_cdi=90.00 if tipo == "LCI" else 100.00,
                    default_ipca_pre=5.50 if tipo == "LCI" else 6.00,
                )

            if usar_trajetoria_copom and trajetoria_selic:
                if tipo in ("LCI", "CDB") and subtipo == "CDI":
                    percentual_cdi = pct_to_decimal(st.session_state[f"{key_prefix}_pct_cdi"])
                    trajetoria_produto = [(d, nivel * percentual_cdi) for d, nivel in trajetoria_cdi_global]
                    retorno = retorno_termo(data_inicio, data_fim, calendario_nome, trajetoria_produto)
                    taxa_aa = taxa_anual_equivalente(retorno, DU)
                    card_trajetoria = trajetoria_produto
                    st.caption(f"Taxa efetiva pela trajetória do Copom: {formata_pct(taxa_aa)} a.a.")
                elif tipo == "TESOURO" and subtipo == "Tesouro Selic":
                    spread = pct_to_decimal(st.session_state[f"{key_prefix}_tesouro_selic_spread"])
                    trajetoria_produto = [(d, taxa_composta(nivel, spread)) for d, nivel in trajetoria_selic]
                    retorno = retorno_termo(data_inicio, data_fim, calendario_nome, trajetoria_produto)
                    taxa_aa = taxa_anual_equivalente(retorno, DU)
                    card_trajetoria = trajetoria_produto
                    st.caption(f"Taxa efetiva pela trajetória do Copom: {formata_pct(taxa_aa)} a.a.")
        else:
            taxa_liq = pct_to_decimal(
                st.number_input(
                    "Taxa líquida a.a. (%)",
                    value=12.00,
                    step=0.10,
                    format="%.4f",
                    key=f"{key_prefix}_liq",
                )
            )
            taxa_aa = taxa_bruta_por_taxa_liquida(
                tipo,
                taxa_liq,
                valor_inicial,
                DU,
                DC,
                taxa_custodia_aa,
                subtipo if tipo == "TESOURO" else "Tesouro Prefixado",
                aplicar_isencao_selic_10k,
            )

    cards.append(
        {
            "id": pid,
            "tipo": tipo,
            "subtipo": subtipo,
            "rotulo": rotulo,
            "taxa_aa": taxa_aa,
            "trajetoria": card_trajetoria,
        }
    )

rotulos_finais = rotulos_unicos([card["rotulo"] for card in cards])
for card, rotulo_final in zip(cards, rotulos_finais):
    card["rotulo"] = rotulo_final

resultados = [
    calcula_produto(
        card["tipo"],
        card["rotulo"],
        valor_inicial,
        card["taxa_aa"],
        DU,
        DC,
        taxa_custodia_aa,
        card.get("subtipo", "Tesouro Prefixado"),
        aplicar_isencao_selic_10k,
    )
    for card in cards
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
    produtos_calc=cards,
    data_inicio=data_inicio,
    data_fim=data_fim,
    calendario_nome=calendario_nome,
    taxa_custodia_aa=taxa_custodia_aa,
    aplicar_isencao_selic_10k=aplicar_isencao_selic_10k,
)

tickvals, ticktext = gera_ticks_meses_pt(data_inicio, data_fim)

fig_valor_liquido = go.Figure()
for nome_produto, grupo in serie.groupby("Produto", sort=False):
    fig_valor_liquido.add_trace(
        go.Scatter(
            x=grupo["Data"],
            y=grupo["Valor Líquido"],
            name=nome_produto,
            mode="lines",
            line=dict(width=1.8, shape="spline", smoothing=0.6),
            hovertemplate="R$ %{y:,.2f}<extra></extra>",
        )
    )

fig_valor_liquido.update_layout(
    height=420,
    hovermode="x unified",
    legend_title_text="Produto",
    margin=dict(l=10, r=10, t=10, b=10),
    yaxis=dict(title="Valor Líquido (R$)", range=[valor_inicial, serie["Valor Líquido"].max() * 1.005], tickformat=",.2f"),
    xaxis=dict(title="Data", tickmode="array", tickvals=tickvals, ticktext=ticktext, hoverformat="%d/%m/%Y"),
)
st.plotly_chart(fig_valor_liquido, use_container_width=True)

st.subheader("Equivalência em relação à LCI")

cards_lci = [card for card in cards if card["tipo"] == "LCI"]

if not cards_lci:
    st.caption("Adicione um produto do tipo LCI para ver a equivalência.")
else:
    base_lci = cards_lci[0]
    base_key_prefix = f"p{base_lci['id']}"
    taxa_liquida_lci = df.loc[df["Produto"] == base_lci["rotulo"], "Taxa Líquida a.a."].iloc[0]
    outros_cards = [card for card in cards if card["id"] != base_lci["id"]]

    if not outros_cards:
        st.caption("Adicione outro produto para comparar a equivalência com a LCI.")
    else:
        indexador_lci = base_lci["subtipo"]

        # Tesouro segue o indexador da LCI de referência para a bisseção, como no modelo original
        # (comparação "traduzida" para a linguagem do indexador da LCI, independentemente do
        # título específico escolhido para cada card de Tesouro).
        subtipo_tesouro_ref = None
        if modo == "Informar taxa bruta":
            if indexador_lci == "CDI":
                subtipo_tesouro_ref = "Tesouro Selic"
            elif indexador_lci == "IPCA+":
                subtipo_tesouro_ref = "Tesouro IPCA+"

        cdi_aa_ref = None
        selic_ref = None
        ipca_ref = None

        if modo == "Informar taxa bruta" and indexador_lci == "CDI":
            cdi_aa_ref = busca_cdi_aa()
            if cdi_aa_ref is None:
                manual_key = f"{base_key_prefix}_cdi_manual"
                if manual_key in st.session_state:
                    cdi_aa_ref = pct_to_decimal(st.session_state[manual_key])

            selic_ref = busca_selic_meta_aa()
            if selic_ref is None:
                for card in outros_cards:
                    manual_key = f"p{card['id']}_tesouro_selic_taxa"
                    if manual_key in st.session_state:
                        selic_ref = pct_to_decimal(st.session_state[manual_key])
                        break
        elif modo == "Informar taxa bruta" and indexador_lci == "IPCA+":
            ipca_key = f"{base_key_prefix}_ipca_est"
            if ipca_key in st.session_state:
                ipca_ref = pct_to_decimal(st.session_state[ipca_key])

        colunas_equiv = st.columns(len(outros_cards))
        for coluna, card in zip(colunas_equiv, outros_cards):
            tipo = card["tipo"]
            eh_tesouro = tipo == "TESOURO"
            subtipo_bissecao = subtipo_tesouro_ref if (eh_tesouro and subtipo_tesouro_ref) else card["subtipo"]

            taxa_equiv = taxa_bruta_por_taxa_liquida(
                tipo,
                taxa_liquida_lci,
                valor_inicial,
                DU,
                DC,
                taxa_custodia_aa,
                subtipo_bissecao,
                aplicar_isencao_selic_10k,
            )

            rotulo_produto = subtipo_tesouro_ref if (eh_tesouro and subtipo_tesouro_ref) else card["rotulo"]
            rotulo_metric = f"{rotulo_produto} bruto equivalente à LCI"
            valor_metric = formata_pct(taxa_equiv)

            if cdi_aa_ref and not eh_tesouro:
                rotulo_metric = f"{card['rotulo']} equivalente à LCI"
                valor_metric = f"{formata_pct(percentual_sobre_indice(taxa_equiv, cdi_aa_ref))} do CDI"
            elif selic_ref and eh_tesouro:
                rotulo_metric = "Tesouro Selic equivalente à LCI"
                valor_metric = f"Selic + {formata_pct(spread_sobre_indice(taxa_equiv, selic_ref))}"
            elif ipca_ref:
                rotulo_metric = f"{rotulo_produto} equivalente à LCI"
                valor_metric = f"IPCA + {formata_pct(spread_sobre_indice(taxa_equiv, ipca_ref))}"

            coluna.metric(rotulo_metric, valor_metric)

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
        - **Trajetória Selic (Copom), parâmetro avançado:** quando ativada, substitui a Selic/CDI
          constante por uma trajetória construída reunião a reunião do Copom, com ajustes de
          ±0,25 p.p. (ou múltiplos) informados pelo usuário. O CDI é projetado acompanhando
          integralmente as variações da Selic, mantendo o spread atual entre os dois. Aplica-se
          apenas no modo "Informar taxa bruta", a produtos indexados a CDI e ao Tesouro Selic;
          os demais indexadores não são afetados. As datas das reuniões seguem o calendário
          oficial divulgado pelo Banco Central quando disponível; além dele, são estimadas por
          ciclo de 45 dias.
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

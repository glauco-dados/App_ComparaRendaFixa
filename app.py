from __future__ import annotations

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
        "Produto": tipo if tipo != "TESOURO" else produto_tesouro,
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
        "LCI": (1 + taxa_lci) ** (1 / DIAS_UTEIS_ANO) - 1,
        "CDB": (1 + taxa_cdb) ** (1 / DIAS_UTEIS_ANO) - 1,
        produto_tesouro: (1 + taxa_tesouro) ** (1 / DIAS_UTEIS_ANO) - 1,
    }
    saldo = {nome: valor_inicial for nome in taxa_dia}
    custodia_acumulada = 0.0

    linhas: list[dict[str, Any]] = []
    d = data_inicio
    while d <= data_fim:
        if cal.isbizday(data_iso(d)):
            for nome in saldo:
                saldo[nome] *= 1 + taxa_dia[nome]

            base_custodia = saldo[produto_tesouro]
            if produto_tesouro == "Tesouro Selic" and aplicar_isencao_selic_10k:
                base_custodia = max(saldo[produto_tesouro] - 10_000, 0)
            custodia_acumulada += base_custodia * taxa_dia_custodia

            aliquota = aliquota_ir(dias_corridos(data_inicio, d))

            valor_liquido_lci = saldo["LCI"]
            valor_liquido_cdb = saldo["CDB"] - max(saldo["CDB"] - valor_inicial, 0) * aliquota
            valor_liquido_tesouro = (
                saldo[produto_tesouro]
                - max(saldo[produto_tesouro] - valor_inicial, 0) * aliquota
                - custodia_acumulada
            )

            linhas.append({"Data": d, "Produto": "LCI", "Valor Líquido": valor_liquido_lci})
            linhas.append({"Data": d, "Produto": "CDB", "Valor Líquido": valor_liquido_cdb})
            linhas.append({"Data": d, "Produto": produto_tesouro, "Valor Líquido": valor_liquido_tesouro})
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
    data_fim = st.date_input(
        "Data de vencimento/resgate",
        value=date.today() + timedelta(days=366),
        format="DD/MM/YYYY",
    )

    produto_tesouro = st.selectbox(
        "Tipo de Tesouro",
        ["Tesouro Prefixado", "Tesouro IPCA+", "Tesouro Selic"],
        index=0,
    )
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

taxa_tesouro_default = 14.00

if modo == "Informar taxa bruta":
    col1, col2, col3 = st.columns(3)
    taxa_lci = pct_to_decimal(col1.number_input("LCI - taxa bruta a.a. (%)", value=12.00, step=0.10, format="%.4f"))
    taxa_cdb = pct_to_decimal(col2.number_input("CDB - taxa bruta a.a. (%)", value=14.00, step=0.10, format="%.4f"))
    taxa_tesouro = pct_to_decimal(col3.number_input(f"{produto_tesouro} - taxa bruta a.a. (%)", value=taxa_tesouro_default, step=0.10, format="%.4f"))
else:
    col1, col2, col3 = st.columns(3)
    taxa_lci_liq = pct_to_decimal(col1.number_input("LCI - taxa líquida a.a. (%)", value=12.00, step=0.10, format="%.4f"))
    taxa_cdb_liq = pct_to_decimal(col2.number_input("CDB - taxa líquida a.a. (%)", value=12.00, step=0.10, format="%.4f"))
    taxa_tesouro_liq = pct_to_decimal(col3.number_input(f"{produto_tesouro} - taxa líquida a.a. (%)", value=12.00, step=0.10, format="%.4f"))

    taxa_lci = taxa_bruta_por_taxa_liquida("LCI", taxa_lci_liq, valor_inicial, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k)
    taxa_cdb = taxa_bruta_por_taxa_liquida("CDB", taxa_cdb_liq, valor_inicial, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k)
    taxa_tesouro = taxa_bruta_por_taxa_liquida("TESOURO", taxa_tesouro_liq, valor_inicial, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k)

resultados = [
    calcula_produto("LCI", valor_inicial, taxa_lci, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k),
    calcula_produto("CDB", valor_inicial, taxa_cdb, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k),
    calcula_produto("TESOURO", valor_inicial, taxa_tesouro, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k),
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
    data_inicio=data_inicio,
    data_fim=data_fim,
    calendario_nome=calendario_nome,
    taxa_custodia_aa=taxa_custodia_aa,
    produto_tesouro=produto_tesouro,
    aplicar_isencao_selic_10k=aplicar_isencao_selic_10k,
)

grafico_valor_liquido = (
    alt.Chart(serie)
    .mark_line(point=True)
    .encode(
        x=alt.X("Data:T", title="Data"),
        y=alt.Y("Valor Líquido:Q", title="Valor Líquido (R$)", axis=alt.Axis(format=",.2f")),
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
base_lci = df[df["Produto"] == "LCI"].iloc[0]
cdb_equiv = taxa_bruta_por_taxa_liquida("CDB", base_lci["Taxa Líquida a.a."], valor_inicial, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k)
tesouro_equiv = taxa_bruta_por_taxa_liquida("TESOURO", base_lci["Taxa Líquida a.a."], valor_inicial, DU, DC, taxa_custodia_aa, produto_tesouro, aplicar_isencao_selic_10k)

col_a, col_b = st.columns(2)
col_a.metric("CDB bruto equivalente à LCI", formata_pct(cdb_equiv))
col_b.metric(f"{produto_tesouro} bruto equivalente à LCI", formata_pct(tesouro_equiv))

with st.expander("Detalhes metodológicos"):
    st.markdown(
        """
        - **Calendário:** cálculo de dias úteis via `bizdays`, sempre pelo calendário ANBIMA.
        - **Base anual:** 252 dias úteis.
        - **LCI:** considerada isenta de IR para pessoa física.
        - **CDB:** IR regressivo sobre o rendimento bruto.
        - **Tesouro Direto:** IR regressivo sobre o rendimento bruto e taxa de custódia B3 pró-rata diária.
        - **Tesouro Selic:** opção para aplicar isenção de custódia até R$ 10 mil por CPF.
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

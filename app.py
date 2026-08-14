from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import streamlit as st
from bizdays import Calendar

st.set_page_config(page_title="Comparador LCI x CDB x Tesouro", layout="wide")

# =========================================================
# Constantes financeiras
# =========================================================
DIAS_UTEIS_ANO = 252
TAXA_CUSTODIA_TESOURO_AA_PADRAO = 0.002  # 0,20% a.a.

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


def ajustar_data_util(d: date, nome_calendario: str, convencao: str) -> date:
    cal = carrega_calendario(nome_calendario)
    if convencao == "Following":
        return cal.following(data_iso(d))
    if convencao == "Preceding":
        return cal.preceding(data_iso(d))
    return d


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
        "Custódia B3": custodia,
        "Valor Líquido": valor_liquido,
        "Rendimento Líquido": rendimento_liquido,
        "Taxa Líquida a.a.": taxa_liquida_aa,
        "IR %": aliquota,
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


# =========================================================
# Leitura opcional do Excel enviado pelo usuário
# =========================================================
def extrair_numero_apos_rotulo(df: pd.DataFrame, rotulo: str) -> float | None:
    valores = df.astype(object).values.tolist()
    linhas = len(valores)
    colunas = len(valores[0]) if linhas else 0

    for i in range(linhas):
        for j in range(colunas):
            if str(valores[i][j]).strip().lower() == rotulo.lower():
                candidatos = []
                if j + 1 < colunas:
                    candidatos.append(valores[i][j + 1])
                if i + 1 < linhas:
                    candidatos.append(valores[i + 1][j])
                for c in candidatos:
                    try:
                        if pd.notna(c):
                            return float(c)
                    except Exception:
                        pass
    return None


def carregar_defaults_excel(arquivo) -> dict[str, float]:
    try:
        df = pd.read_excel(arquivo, sheet_name=0, header=None, engine="openpyxl")
    except Exception:
        return {}

    defaults = {}
    valor_inicial = extrair_numero_apos_rotulo(df, "Valor Inicial:")
    if valor_inicial is not None:
        defaults["valor_inicial"] = valor_inicial

    # Busca a primeira taxa de título encontrada no arquivo como sugestão para Tesouro.
    taxa_titulo = extrair_numero_apos_rotulo(df, "Taxa do Título:")
    if taxa_titulo is not None:
        defaults["taxa_tesouro"] = taxa_titulo * 100

    return defaults


# =========================================================
# Interface Streamlit
# =========================================================
st.title("Comparador LCI x CDB x Tesouro Direto")
st.caption(
    "Comparação de rentabilidade líquida com calendário financeiro ANBIMA/B3 via bizdays, "
    "IR regressivo e taxa de custódia do Tesouro Direto."
)

with st.sidebar:
    st.header("Entrada de dados")
    arquivo_excel = st.file_uploader(
        "Excel base opcional",
        type=["xlsx"],
        help="Você pode carregar a planilha TESOURO VS LCI.xlsx apenas para sugerir alguns valores iniciais.",
    )

    defaults = carregar_defaults_excel(arquivo_excel) if arquivo_excel else {}

    st.subheader("Calendário")
    calendario_nome = st.selectbox(
        "Calendário bizdays",
        ["ANBIMA", "B3", "Actual"],
        index=0,
        help="ANBIMA é o calendário padrão do mercado brasileiro de renda fixa.",
    )
    convencao_vencimento = st.selectbox(
        "Ajuste da data final",
        ["Nenhum", "Following", "Preceding"],
        index=0,
        help="Following ajusta para o próximo dia útil; Preceding ajusta para o dia útil anterior.",
    )

    st.subheader("Parâmetros gerais")
    valor_inicial = st.number_input(
        "Valor inicial",
        min_value=0.0,
        value=float(defaults.get("valor_inicial", 100000.0)),
        step=1000.0,
        format="%.2f",
    )
    data_inicio = st.date_input("Data da aplicação", value=date.today())
    data_fim_original = st.date_input("Data de vencimento/resgate", value=date.today() + timedelta(days=366))
    data_fim = ajustar_data_util(data_fim_original, calendario_nome, convencao_vencimento)

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

try:
    DU = dias_uteis_bizdays(data_inicio, data_fim, calendario_nome)
except Exception as exc:
    st.error(f"Não foi possível carregar o calendário '{calendario_nome}' pelo bizdays. Detalhe: {exc}")
    st.stop()

DC = dias_corridos(data_inicio, data_fim)
ALIQUOTA = aliquota_ir(DC)

if data_fim != data_fim_original:
    st.info(f"Data final ajustada pela convenção {convencao_vencimento}: {data_fim_original:%d/%m/%Y} -> {data_fim:%d/%m/%Y}")

k1, k2, k3, k4 = st.columns(4)
k1.metric("Dias corridos", DC)
k2.metric("Dias úteis", DU)
k3.metric("Alíquota IR", formata_pct(ALIQUOTA))
k4.metric("Calendário", calendario_nome)

if DU <= 0:
    st.warning("Informe uma data final posterior à data inicial.")
    st.stop()

st.subheader("Taxas dos produtos")

taxa_tesouro_default = float(defaults.get("taxa_tesouro", 14.00))

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
g1, g2 = st.columns(2)
with g1:
    st.bar_chart(df.set_index("Produto")[["Valor Líquido"]])
with g2:
    st.bar_chart(df.set_index("Produto")[["Taxa Líquida a.a."]])

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
        - **Calendário:** cálculo de dias úteis via `bizdays`, com opção ANBIMA, B3 ou Actual.
        - **Base anual:** 252 dias úteis.
        - **LCI:** considerada isenta de IR para pessoa física.
        - **CDB:** IR regressivo sobre o rendimento bruto.
        - **Tesouro Direto:** IR regressivo sobre o rendimento bruto e taxa de custódia B3 pró-rata diária.
        - **Tesouro Selic:** opção para aplicar isenção de custódia até R$ 10 mil por CPF.
        - **Limitação:** para Tesouro Prefixado/IPCA+ vendido antes do vencimento, o app não modela marcação a mercado.
        """
    )

csv = df.to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig")
st.download_button(
    "Baixar resultado em CSV",
    data=csv.encode("utf-8-sig"),
    file_name="comparativo_lci_cdb_tesouro.csv",
    mime="text/csv",
)

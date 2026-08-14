# Comparador LCI x CDB x Tesouro Direto

Aplicação em Streamlit para comparar alternativas de renda fixa no mercado brasileiro, usando calendário financeiro via `bizdays`.

## O que a aplicação faz

- Compara LCI, CDB e Tesouro Direto.
- Calcula dias corridos e dias úteis automaticamente.
- Usa calendário `ANBIMA`, `B3` ou `Actual` via `bizdays`.
- Permite comparar pela taxa bruta informada.
- Permite comparar pela taxa líquida desejada, calculando a taxa bruta necessária.
- Aplica IR regressivo para CDB e Tesouro.
- Considera taxa de custódia B3 para Tesouro Direto.
- Permite aplicar a isenção de custódia de até R$ 10 mil para Tesouro Selic.
- Exporta o resultado em CSV.

## Arquivos do projeto

```text
app.py
requirements.txt
runtime.txt
.streamlit/config.toml
README.md
```

## Como publicar usando a versão web do Streamlit

Este fluxo é indicado quando você não consegue executar `streamlit.exe` localmente.

1. Crie um repositório no GitHub, por exemplo: `comparador-lci-cdb-tesouro`.
2. Envie para o repositório os arquivos deste pacote:
   - `app.py`
   - `requirements.txt`
   - `runtime.txt`
   - pasta `.streamlit` com o arquivo `config.toml`
3. Acesse: https://share.streamlit.io ou https://streamlit.io/cloud
4. Entre com sua conta GitHub.
5. Clique em **Create app** ou **New app**.
6. Selecione:
   - Repository: seu repositório GitHub
   - Branch: `main`
   - Main file path: `app.py`
7. Clique em **Deploy**.

O Streamlit Community Cloud instalará as dependências do `requirements.txt` automaticamente.

## Premissas do modelo

1. As taxas são tratadas como efetivas ao ano.
2. A capitalização usa 252 dias úteis.
3. A LCI é considerada isenta de IR para pessoa física.
4. CDB e Tesouro Direto usam tabela regressiva de IR sobre o rendimento.
5. A taxa de custódia do Tesouro Direto é simulada como provisão diária pró-rata.
6. Para Tesouro Prefixado e IPCA+ vendido antes do vencimento, o modelo não calcula marcação a mercado.

## Tabela de IR utilizada

| Prazo em dias corridos | Alíquota |
|---:|---:|
| Até 180 | 22,5% |
| 181 a 360 | 20,0% |
| 361 a 720 | 17,5% |
| Acima de 720 | 15,0% |

## Observação sobre o Excel base

O aplicativo permite carregar o arquivo `TESOURO VS LCI.xlsx` de forma opcional para sugerir alguns valores iniciais, mas a comparação é feita diretamente na interface.

## Próximas melhorias sugeridas

- Adicionar produtos em `% CDI`.
- Adicionar produtos `IPCA + spread`.
- Capturar CDI/Selic via API ou Selenium, conforme restrições de rede.
- Gerar relatório HTML ou PDF para envio ao cliente.
- Permitir comparação com curva de vencimentos de Tesouro Direto.

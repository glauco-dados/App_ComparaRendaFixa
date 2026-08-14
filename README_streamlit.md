# Comparador LCI x CDB x Tesouro Direto

Aplicação em Streamlit para comparar alternativas de renda fixa no mercado brasileiro, usando calendário financeiro via `bizdays`.

## O que a aplicação faz

- Compara um número livre de produtos (LCI, CDB e Tesouro Direto), podendo incluir mais de uma oferta do mesmo tipo — basta clicar em "+ Adicionar produto" e remover o que não precisar.
- Calcula dias corridos e dias úteis automaticamente.
- Usa calendário `ANBIMA`, `B3` ou `Actual` via `bizdays`.
- Permite comparar pela taxa bruta informada.
- Permite comparar pela taxa líquida desejada, calculando a taxa bruta necessária.
- Aplica IR regressivo para CDB e Tesouro.
- Considera taxa de custódia B3 para Tesouro Direto.
- Permite aplicar a isenção de custódia de até R$ 10 mil para Tesouro Selic.
- Mostra a "Equivalência em relação à LCI": a taxa bruta que cada outro produto precisaria oferecer para igualar a taxa líquida da LCI, sempre traduzida para o indexador da LCI (ex.: se a LCI é CDI, CDB e Tesouro aparecem como "% do CDI" e "Selic + spread", mesmo que o Tesouro tenha sido configurado como Prefixado ou IPCA+).
- Rótulo de cada produto se atualiza automaticamente ao trocar tipo ou indexador (pode ser personalizado livremente).
- Parâmetro avançado: trajetória da Selic reunião a reunião do Copom, com ajustes de ±0,25 p.p. por reunião, usada para recalcular as taxas de produtos indexados a CDI e Tesouro Selic (Selic + spread) ao longo do período — em vez de assumir a Selic/CDI constante.
- Exporta o resultado em CSV e em PDF.

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
7. O calendário de reuniões do Copom está embutido no código (2026 e 2027, conforme divulgado pelo Banco Central); datas além desse horizonte são estimadas por ciclo de 45 dias e precisarão ser atualizadas manualmente quando o BC divulgar novos calendários.
8. Na trajetória da Selic via Copom, assume-se que o CDI acompanha integralmente as variações da Selic, mantendo o spread atualmente vigente entre os dois.

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

- Marcação a mercado para Tesouro Prefixado/IPCA+ vendido antes do vencimento.
- Poupança como produto de referência adicional.
- Comparação com curva de vencimentos de Tesouro Direto (mesmo título, prazos diferentes).

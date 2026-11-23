---
name: analise-financeira
description: Roteiro para analisar carteira real e extratos, comparar com Selic/CDI do Banco Central e registrar propostas em 03-Financas/Aportes.md com aprovacao humana.
---

# Skill: Analise Financeira e Proposta de Aporte

Siga este roteiro passo a passo sempre que o usuario pedir analise da carteira, revisao de extratos, comparacao com CDI/Selic ou sugestao de aporte:

## 1. Coletar dados reais antes de opinar
- Delegue para `delegar_para_investimentos` consultando obrigatoriamente:
  - `ver_carteira_real()` para patrimonio atual, alocacao por categoria e resumo do extrato importado.
  - `obter_selic_cdi_bcb()` para a taxa Selic/CDI real do Banco Central e referencia de rendimento a 100% CDI.
  - `buscar_noticias_mercado()` para contexto macroeconomico recente.
  - `consultar_cotacao(ticker)` para qualquer ativo especifico mencionado (nunca invente precos ou taxas).

## 2. Avaliar risco, liquidez e retorno vs. 100% CDI
- Verifique concentracao por classe (`conta_cdi`, `renda_fixa`, `acoes_fiis`, `etf`, `cripto`) e reserva de liquidez imediata.
- Compare explicitamente cada alternativa contra o benchmark **100% do CDI** (retorno esperado, volatilidade, liquidez e risco de perda de principal).
- Destaque o nivel de risco de forma transparente (`baixo`, `medio` ou `alto`).

## 3. Regra inviolavel de aprovacao humana
- Nunca sugira que a Athena movimentou ou vai movimentar dinheiro automaticamente.
- Caso haja recomendacao concreta de aporte, registre a sugestao usando `propor_investimento(...)` para que ela apareca como **PENDENTE** na aba **Carteira** da Athena.
- Reforce claramente que a execucao final no banco/corretora (ex.: Mercado Pago) e sempre manual pelo usuario apos aprovacao.

## 4. Registrar historico no Obsidian (`03-Financas/Aportes.md`)
- Documente o resumo da analise (data, Selic/CDI vigente, diagnostico da carteira e propostas sugeridas) em `03-Financas/Aportes.md` via `escrever_nota_vault(..., anexar=True)` usando `#financas` e `[[wikilinks]]`.
- Entregue ao usuario uma tabela/resumo objetivo com os numeros reais e os proximos passos.

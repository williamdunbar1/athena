---
name: plano-estudo-prova
description: Roteiro estruturado para revisar provas A1/A2 da faculdade cruzando prazos do Ulife, busca hibrida nos PDFs (Qdrant) e salvando o guia no Obsidian.
---

# Skill: Plano de Estudo para Prova (A1 / A2)

Siga este roteiro passo a passo sempre que o usuario pedir um plano de estudos, revisao ou preparacao para prova (A1, A2, A3 ou entrega academica):

## 1. Mapear prazos, materias e pendencias reais
- Delegue para `delegar_para_estudos` consultando:
  - `consultar_dados_ulife` (leitura rapida do banco para notas atuais, faltas, atividades e avisos da disciplina — so sincronize o Ulife se o usuario pedir ou se o banco estiver vazio).
  - `ver_agenda_provas` para identificar a data exata da prova e quantos dias restam.
  - `ver_materias_e_aulas` para listar todos os PDFs disponiveis e quais aulas estao pendentes ou marcadas para revisar.

## 2. Extrair conteudo-chave com Busca Hibrida (Qdrant + PDFs)
- Use `buscar_semantico` (Qdrant hibrido denso + BM25 + reranker, com `fonte="pdf"`) para localizar os topicos centrais cobrados na prova, formulas, teoremas e definicoes tecnicas.
- Quando precisar aprofundar plano de ensino, criterios de avaliacao ou exercicios resolvidos, peça ao `Agente Estudos` para ler paginas especificas com `ler_paginas_de_pdf`.
- Sempre cite a origem exata de cada topico no formato `[arquivo.pdf, pag. X]`.

## 3. Montar o guia de revisao (cronograma reverso)
- Organize o plano por prioridade (peso na prova + aulas ainda nao estudadas):
  1. **Resumo das datas e meta de nota** (quanto falta para media, se houver notas do Ulife).
  2. **Mapa de topicos essenciais** com formulas/conceitos-chave e referencias `[arquivo.pdf, pag. X]`.
  3. **Cronograma diario reverso** ate a vespera da prova (blocos curtos de teoria + pratica + revisao ativa).
  4. **Mini-simulado / checklist de autoavaliacao** com 5 a 8 perguntas objetivas ou exercicios-tipo para testar dominio.

## 4. Persistir no Segundo Cerebro (Obsidian)
- Salve o guia final em `01-Faculdade/Plano-Estudo-<Materia>-<Prova>.md` usando `escrever_nota_vault`, incluindo frontmatter/tags (`#faculdade`, `#prova`, `#plano-estudos`) e `[[wikilinks]]` para `[[01-Faculdade/Painel-Ulife]]`.
- Devolva ao usuario um resumo claro do plano criado e informe o caminho exato da nota salva no Obsidian.

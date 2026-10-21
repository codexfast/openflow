[English](CONTRIBUTING.md) | Português (Brasil)

# Contribuindo com o OpenFlow

Contribuições de código, testes e documentação são bem-vindas. Você pode participar em **português ou inglês**; não precisa de uma conta Google ou de créditos do Flow para começar.

## Por onde começar

- Veja as [primeiras contribuições](https://github.com/codexfast/openflow/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22) e as [tarefas que precisam de ajuda](https://github.com/codexfast/openflow/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22help%20wanted%22).
- Comente na issue antes de começar para combinar o escopo e evitar trabalho duplicado.
- Para uma ideia nova, procure uma [issue existente](https://github.com/codexfast/openflow/issues) antes de abrir outra. Mudanças maiores devem ser discutidas antes da implementação.
- Melhorias de documentação, exemplos sanitizados e testes com mocks são boas formas de contribuir sem acessar o Google Flow.

## Idiomas da documentação

- O inglês (EN) é o idioma principal do projeto para documentação e referências técnicas.
- Mantenha a documentação em inglês e português atualizada em conjunto para alterações de código, comportamento ou arquitetura.
- Contribuições, issues e pull requests são aceitos em inglês ou português.
- Nenhuma contribuição exige conta Google, créditos reais do Flow ou execução de gerações pagas; use testes com mocks e dados sintéticos.

## Preparar o ambiente

Requisitos: **Python 3.11 ou superior**, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git e Node.js com suporte a `node --test` para a suíte JavaScript. A instalação das dependências precisa de acesso à rede.

Faça um fork pelo GitHub e clone o seu fork, substituindo `SEU_USUARIO`:

```bash
git clone https://github.com/SEU_USUARIO/openflow.git
```

```bash
cd openflow
```

Crie uma branch para a contribuição:

```bash
git switch -c minha-contribuicao
```

Instale as dependências de desenvolvimento:

```bash
uv sync --locked --extra browser --extra dev
```

Essa preparação não exige Chromium, login Google nem configuração MCP.

## Verificações locais

Execute as verificações pertinentes à mudança, na raiz do repositório:

### Testes Python

```bash
uv run --extra browser --extra dev pytest
```

### Lint

```bash
uv run --extra browser --extra dev ruff check .
```

### Tipagem

```bash
uv run --extra browser --extra dev mypy
```

### Testes JavaScript

```bash
node --test extension/tests/*.test.mjs
```

Os testes automatizados usam mocks, dados temporários e transporte ASGI em memória; não precisam de uma sessão autenticada ou de gerações reais. A suíte completa precisa do pacote Playwright incluído no extra `browser`: sem ele, alguns testes são pulados e outros falham por dependência ausente.

**Instalar o pacote Playwright não exige baixar o Chromium nem fazer login para executar esses mocks.** Mantenha os extras `browser` e `dev` nos comandos Python, pois o uv pode sincronizar o ambiente e remover extras não selecionados.

Para rodar apenas um arquivo de testes, por exemplo:

```bash
uv run --extra browser --extra dev pytest tests/test_api.py
```

Se uma verificação falhar antes das suas mudanças, registre o comando, a revisão e o erro sanitizado no PR. Compare com a branch-base quando necessário. Não desative testes nem afrouxe lint ou tipagem apenas para obter um resultado verde.

## Estrutura do projeto

| Caminho | Responsabilidade |
| --- | --- |
| `src/flow_mcp/server.py` | Interface MCP |
| `src/flow_mcp/api.py` | Interface REST |
| `src/flow_mcp/service.py` | Operações compartilhadas e downloads |
| `src/flow_mcp/browser.py` e `managed.py` | Perfil Chromium, sessão e backend gerenciado |
| `src/flow_mcp/jobs.py` | Persistência dos jobs |
| `tests/` | Testes Python, mocks e fixtures |
| `extension/` | Backend legado e runtime JavaScript também usado pelo backend gerenciado |
| `extension/tests/` | Testes JavaScript |

Não trate `extension/` como código descartável: `operations.mjs` e `hijack_bypass.js` também são incluídos no pacote Python e usados pelo navegador gerenciado.

## Testes reais no Google Flow: opcionais

Testes reais são diferentes da suíte com mocks. Só os execute quando forem necessários à mudança, com uma conta que você tenha autorização para usar. Consulte [instalação e login](README.pt-BR.md#setup) e [segurança, conta e créditos](README.pt-BR.md#security) antes de instalar Chromium ou executar `openflow setup`.

- Informe senha e 2FA somente no site Google, nunca em uma issue, prompt ou arquivo do projeto.
- MCP, REST e setup não devem disputar o mesmo perfil simultaneamente.
- Operações de geração e upscale podem consumir créditos. Apresente as entradas, obtenha aprovação humana e respeite a sequência de prévia e confirmação.
- Não reenvie automaticamente jobs com resultado incerto, como `submitted_unverified`.
- Não automatize operações pagas na CI nem exija que outras pessoas gastem créditos para validar um PR.

OpenFlow não é uma API oficial do Google. O uso de interfaces internas pode contrariar termos do serviço e causar restrições à conta; mudanças no Flow podem interromper a integração.

## Abrir uma issue

Para bugs, inclua:

- revisão ou versão do OpenFlow, sistema operacional e versão do Python;
- backend usado (gerenciado ou extensão) e interface (MCP ou REST);
- versões de Playwright/Chromium, quando forem relevantes;
- passos mínimos de reprodução, resultado esperado e resultado observado;
- erros e logs sanitizados, sem dados de autenticação.

Para sugestões, explique o problema, o caso de uso e as alternativas consideradas. Não é necessário executar uma geração paga para abrir uma issue.

## Enviar um pull request

1. Mantenha o PR focado em uma tarefa; evite refatorações ou arquivos gerados sem relação com ela.
2. Siga os nomes, tipos e estilo do código ao redor. Para mudanças de comportamento, adicione ou ajuste testes com mocks e fixtures sintéticas.
3. Execute as verificações pertinentes e informe quais passaram, falharam ou não foram executadas, com o motivo.
4. Descreva o que mudou, por quê e como testar. Relacione a issue, quando houver, e atualize a documentação se a interface ou o comportamento mudou.
5. Revise o diff antes de enviar. Não inclua segredos, perfis de navegador, bancos de jobs ou mídias geradas.

Confira também problemas de whitespace:

```bash
git diff --check
```

## Privacidade e arquivos locais

Nunca publique senhas, cookies, tokens, códigos de pareamento, perfis Chromium, bancos de jobs, URLs assinadas ou mídias privadas em issues, PRs, capturas ou logs. Use identificadores sintéticos nos testes.

Salve downloads e saídas de teste em `artifacts/`, já ignorado pelo Git. A pasta `assets/` também está ignorada; não use inclusão forçada para enviar gerações ou banners sem relação com o código.

Se encontrar uma possível vulnerabilidade, não publique segredos, dados de terceiros ou detalhes que permitam exploração em uma issue pública. Verifique primeiro se há instruções de comunicação privada na aba **Security** do repositório; este guia não estabelece um canal privado ou prazo de resposta.

As contribuições devem ser compatíveis com a [licença MIT](LICENSE). Preserve os avisos de licença e não inclua código ou mídia de terceiros sem permissão.

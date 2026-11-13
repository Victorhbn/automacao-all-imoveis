# Deploy no Railway

Este projeto usa o `Dockerfile` da raiz e lê a porta da variável `PORT` fornecida pelo Railway. O servidor escuta em `0.0.0.0`; por segurança, ele não inicia em uma interface de rede sem `APP_API_KEY`.

## Corrigir o erro `Configure APP_API_KEY antes de expor o servidor em uma interface de rede.`

1. Gere uma chave forte localmente, por exemplo com Python:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. No Railway, abra o serviço da aplicação e acesse **Variables**.
3. Adicione `APP_API_KEY` e cole a chave gerada como valor. Não coloque a chave no código nem a envie ao GitHub.
4. Salve a variável e faça um novo deploy (ou reinicie o serviço).
5. Abra o domínio público do serviço. Na tela inicial, informe a mesma chave no botão **Acesso**.

`APP_API_KEY` é obrigatória no Railway: não use um valor vazio nem remova essa variável. A configuração de deploy neste repositório usa o Dockerfile e verifica a disponibilidade do serviço em `/`.

## Persistência do banco

Para manter contatos, campanhas e configurações após novos deploys, crie um volume persistente no Railway e monte-o em `/app/data`. O caminho do SQLite já está configurado como `/app/data/disparos.db` no Dockerfile. Mantenha uma única réplica, pois o banco usa SQLite.

## Webhooks Uazapi

Depois de publicar o serviço, copie o domínio HTTPS do Railway e configure-o na tela **Integração** como URL pública. O endpoint de webhook é `<URL pública>/webhooks/uazapi`.

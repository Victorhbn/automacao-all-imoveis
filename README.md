# Disparos — automação para WhatsApp

Aplicação web local para gerenciar contatos, campanhas, respostas automáticas, webhooks e grupos usando a API Uazapi. O backend usa apenas a biblioteca padrão do Python e persiste os dados em SQLite.

## Requisitos

- Python 3.10 ou superior
- Uma instância Uazapi conectada ao WhatsApp

## Deploy no Northflank

O repositório está pronto para build a partir do `Dockerfile` da raiz. No Northflank, conecte o Git provider e crie um **Combined Service** apontando para este repositório e branch:

1. Em **Build options**, selecione **Dockerfile**; use `/Dockerfile` como localização e `/` como build context.
2. Em **Runtime variables**, configure:

   | Variável | Valor |
   | --- | --- |
   | `HOST` | `0.0.0.0` |
   | `PORT` | `8000` |
   | `DATABASE_PATH` | `/app/data/disparos.db` |
   | `APP_API_KEY` | Chave aleatória forte, armazenada como secret |
   | `SEND_INTERVAL_SECONDS` | Opcional; padrão `2` |

   `APP_API_KEY` é obrigatória para iniciar o servidor fora de localhost. Gere uma chave localmente, por exemplo no PowerShell, e copie o valor para um secret no Northflank:

   ```powershell
   $bytes = New-Object byte[] 32
   $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
   $rng.GetBytes($bytes)
   [Convert]::ToBase64String($bytes)
   ```

3. Configure uma porta **HTTP 8000** e habilite **Public** para acessar a aplicação. O `EXPOSE 8000` do Dockerfile permite que o Northflank detecte a porta. O endpoint `/` pode ser usado como health check HTTP.
4. Adicione um volume persistente **Single Read/Write**, com container mount path `/app/data`, e mantenha o serviço em **uma réplica**. O SQLite exige armazenamento persistente e uma única instância; não escale horizontalmente este serviço.
5. Faça o deploy e abra o domínio HTTPS gerado pelo Northflank. No primeiro acesso, use o botão **Acesso** para informar o valor de `APP_API_KEY`; configure Server URL e token da Uazapi na tela **Integração**.

Para webhooks recebidos, informe na Integração a URL pública HTTPS do serviço. O callback configurado na Uazapi será `<URL pública>/webhooks/uazapi`. Mantenha backups do volume persistente: atualizações da imagem não substituem o volume, mas o volume não deve ser tratado como backup.

O serviço é construído pelo Northflank usando as instruções oficiais de [build com Dockerfile](https://northflank.com/docs/v1/application/build/build-with-a-dockerfile), [portas públicas](https://northflank.com/docs/v1/application/network/configure-ports) e [volumes persistentes](https://northflank.com/docs/v1/application/databases-and-persistence/add-a-volume). Para este SQLite, use **Single Read/Write**; esse modo limita o serviço a uma réplica.

## Executar com Docker Compose

Requer Docker Desktop com o Compose v2 ativo. No PowerShell, na pasta do projeto, gere uma chave forte para proteger a aplicação e inicie o container:

```powershell
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$key = [Convert]::ToBase64String($bytes)
Set-Content -Path .env -Value "APP_API_KEY=$key" -NoNewline -Encoding ascii
docker compose up -d --build
docker compose ps
```

Abra `http://127.0.0.1:8000` e informe a chave gerada no botão **Acesso**. Configure o Server URL e o token Uazapi pela tela **Integração**; eles serão armazenados no banco persistente do container. O container escuta na rede interna Docker, mas a porta publicada fica limitada ao localhost do computador.

O banco SQLite fica no volume Docker `disparos_data` e sobrevive a reinicializações e atualizações da imagem. Para acompanhar os logs ou parar a aplicação:

```powershell
docker compose logs -f app
docker compose down
```

Não use `docker compose down -v` a menos que queira apagar permanentemente os dados do volume. O arquivo `.env` contém uma chave secreta: não o compartilhe nem o adicione ao controle de versão. Para alterar a porta local, defina `DISPAROS_PORT` no ambiente antes de iniciar o Compose.

## Executar no Windows

No PowerShell, na pasta do projeto:

```powershell
python app.py
```

Abra `http://127.0.0.1:8000` (não abra o arquivo HTML diretamente) e preencha o **Server URL** e o **token** em **Integração**. A aplicação valida a credencial com a Uazapi antes de salvá-la no SQLite do backend; o token nunca é devolvido à interface. Proteja o arquivo do banco de dados.

Na interface, em **Configurações**, ajuste o intervalo entre mensagens e, se necessário, configure uma chave de acesso para a aplicação. A chave é armazenada no banco local; se `APP_API_KEY` estiver definida no ambiente, ela continua sendo gerenciada pelo servidor e prevalece sobre a interface.

Host (`HOST`), porta (`PORT`) e caminho do banco (`DATABASE_PATH`) são opções de inicialização e exigem reiniciar o servidor para alteração. Por padrão, o SQLite fica em `data\disparos.db`.

## Recursos

- Cadastro manual e importação CSV (`phone`/`telefone`, `name`/`nome` e, opcionalmente, `opted_in`/`consentimento`).
- Grupos internos de contatos: crie, renomeie e exclua grupos, associe-os ao cadastrar ou editar cada contato e use um ou vários grupos para preencher a seleção de destinatários de uma campanha. A seleção continua editável contato por contato e só inclui contatos com consentimento ativo e telefone disponível.
- Edição de nome, telefone e consentimento dos contatos; exclusão apaga os dados pessoais (inclusive os snapshots dos destinatários) e mantém o histórico de campanhas anonimizado.
- Campanhas de texto, imagem e vídeo MP4, com personalização por `{{nome}}`.
- Rascunhos de campanha editáveis; campanhas existentes podem ser reutilizadas como modelo, criando um novo rascunho independente com texto, mídia e destinatários que ainda têm consentimento ativo.
- Botão para reenviar uma campanha já disparada: cria um novo registro de envio, preserva o histórico original e inclui somente os destinatários originais com consentimento ativo e telefone disponível no momento do reenvio.
- Listagem de campanhas com progresso, seleção dos contatos autorizados antes do disparo e painel detalhado do status e erro por contato; “aceito pela API” não garante que a mensagem foi entregue.
- Contatos explicitamente rejeitados pela Uazapi com “not on WhatsApp” são marcados como indisponíveis e excluídos de novos disparos; corrigir o telefone na edição libera nova tentativa. Falhas de conexão da instância, como HTTP 503, não marcam o telefone como inválido.
- Somente contatos com consentimento ativo entram em uma campanha. Contatos coletados de grupos começam sem consentimento.
- Envio sequencial com intervalo configurável na interface (padrão: 2 segundos; `SEND_INTERVAL_SECONDS` define o valor inicial). Falhas não são reenviadas automaticamente.
- Editor visual de workflows: arraste gatilhos, mensagens, mídias e webhooks para o canvas, configure cada bloco, conecte as etapas e edite os fluxos salvos. As etapas conectadas executam na ordem das conexões. Há modelos básicos de boas-vindas e horário de atendimento.
- Listagem de workflows ativos/pausados, com acesso à edição visual.
- Consulta de grupos, importação/consulta de participantes por JID mesmo sem ser administrador, adição de participantes e gerenciamento de grupos em comunidades, conforme as permissões da instância. A importação usa o campo telefônico `PhoneNumber` da Uazapi ou um JID `@s.whatsapp.net`; identificadores LID sem telefone continuam sendo ignorados porque não podem ser convertidos com segurança em números.
- Opt-out quando o contato responde `SAIR`, `PARAR`, `CANCELAR` ou `STOP`.

## Ativar webhooks de mensagens recebidas

Para receber eventos, publique o servidor atrás de um proxy reverso seguro com HTTPS. Informe a URL pública em **Integração** e use **Configurar webhook no Uazapi**. O callback é `/webhooks/uazapi`. A instância Uazapi precisa conseguir acessar esse endpoint pela internet.

## Segurança e consentimento

Ao executar diretamente no Windows, o servidor escuta apenas em `127.0.0.1`. No Docker, ele escuta em `0.0.0.0` dentro do container, exige `APP_API_KEY` definida no arquivo `.env` e publica a porta somente em `127.0.0.1` no host. Para expor a aplicação fora do computador, use HTTPS e autenticação adicional no proxy reverso:

```powershell
$env:APP_API_KEY = "uma-chave-forte-e-aleatoria"
$env:HOST = "0.0.0.0"
```

Em produção, use HTTPS e autenticação adicional no proxy reverso. Proteja o arquivo `data\disparos.db`, que contém credenciais da instância, chave de acesso, dados pessoais e mídias de campanha. Utilize os recursos de contato e grupo somente com autorização apropriada; a importação de participantes não autoriza campanhas.

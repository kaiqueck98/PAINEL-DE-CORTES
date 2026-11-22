# painel-cortes-prontos

Painel web em Python para listar vídeos que estão dentro de uma pasta do Google Drive.

## Requisitos

- Python 3.9 ou superior
- Uma chave de API do Google com a Google Drive API ativada
- A pasta do Drive precisa estar compartilhada como "Qualquer pessoa com o link: leitor". A aplicação usa uma chave de API e não faz login em uma conta Google.

## Executar

1. Instale as dependências:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Configure a chave da API na sessão atual do PowerShell:

   ```powershell
   $env:DRIVE_API_KEY = "sua-chave-de-api"
   ```

   No Google Cloud Console, crie uma chave de API, ative a Google Drive API e, se possível, restrinja a chave à API do Drive.

3. Inicie o painel:

   ```powershell
   python app.py
   ```

4. Abra http://127.0.0.1:5000 e cole o link da pasta.

Ao enviar o link da pasta principal, o painel mostra cartões das subpastas com a contagem recursiva de vídeos. Ao abrir uma pasta, suas subpastas aparecem primeiro e, em seguida, os vídeos diretamente nela. A busca percorre subpastas e reconhece `.mp4`, `.mov`, `.webm` e `.mkv`; falhas isoladas são exibidas como avisos sem interromper a busca nas demais.

Cada vídeo recebe uma nota heurística de 0 a 10 que varia com a duração, as palavras do título, a presença de capa e a marcação como bom. Durações entre 30 e 90 segundos têm maior peso; marcar um vídeo como bom acrescenta pontos. A nota considera a presença da capa, não seu conteúdo visual. Use **Melhores primeiro** para ordenar e **Ver só os bons** para filtrar. O painel também gera sugestões de título e hashtags a partir do nome do arquivo e da pasta, com um botão para copiá-los. As marcações são guardadas por ID de vídeo em `bons.json`, criado ao marcar o primeiro vídeo.

Em cada pasta aberta, os campos **Modelo de título** e **Modelo de hashtags** podem ser salvos para reutilizar suas sugestões em todos os vídeos daquela pasta. O marcador `{numero}` usa o primeiro número no nome do arquivo; se não houver número, usa a posição do arquivo em ordem alfabética. Os modelos ficam em `modelos.json`.

Cada vídeo também pode ser baixado pelo painel ou marcado como postado. Os cartões postados ficam verdes e aparecem no fim da lista; os filtros **Todos**, **Para postar** e **Já postados** controlam a visualização. As marcações são persistidas por ID em `postados.json`.

Vídeos abaixo de 720p mostram **Melhorar qualidade**. O processamento usa FFmpeg local para ajustar o quadro a 1080×1920 sem cortar a imagem, aplicar nitidez e um leve aumento de brilho. O original no Drive não é alterado. Ao terminar, o player do cartão permite comparar **Ver original** e **Ver melhorado**; o arquivo melhorado precisa ser assistido do início ao fim para liberar **Baixar vídeo melhorado**. O resultado é gerado no servidor e baixado para o computador. É necessário ter `ffmpeg` disponível no `PATH` do servidor.

## Persistência das notificações

Em produção, configure `DATABASE_URL` com a conexão PostgreSQL do Supabase para manter as configurações entre reinicializações; no Render, o painel recusa salvar em SQLite local se essa URL estiver ausente. Defina também `SOCIAL_TOKEN_ENCRYPTION_KEY` como uma variável persistente do serviço no Render e mantenha o mesmo valor; sem uma chave estável, o painel bloqueia o salvamento em banco PostgreSQL para evitar gravar credenciais que não poderão ser descriptografadas depois. As variáveis `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` do ambiente são usadas como fallback quando não houver valores salvos no banco. A tela `/agendamento` informa o estado do Telegram, confirma o commit ou apresenta o erro do banco, e oferece **TESTAR TELEGRAM**.

## Rodar automático e título japonês

Configure `DRIVE_API_KEY` e `OPENROUTER_API_KEY`, conecte YouTube e TikTok e instale `ffmpeg` e `ffprobe` no servidor. O botão **RODAR AUTOMÁTICO** transcreve o áudio localmente com Whisper e analisa três frames amostrados com a transcrição para preparar uma prévia. A transcrição e os três frames são enviados ao OpenRouter para gerar a história e os títulos.

A prévia mostra um título japonês de 2 a 4 caracteres com tradução e motivo, além do título em português para publicar. **TROCAR TÍTULO JAPONÊS** solicita uma alternativa sem repetir as anteriores. Depois de gerar a versão com o título centralizado nos primeiros dois segundos, aprove os quatro vídeos para confirmar o agendamento. A mídia enviada é salva como `video_com_titulo.mp4`; a fonte Noto Sans CJK JP é baixada e armazenada em cache na primeira renderização. Se a fonte não puder ser obtida ou não suportar os caracteres, o vídeo não será aprovado nem enviado.

## Conectar TikTok e YouTube; enviar vídeos

Se ainda não existir um `.env`, copie `.env.example` para criá-lo. Se já existir, preserve as configurações atuais e acrescente somente as novas variáveis; preencha as credenciais das duas plataformas. O painel gera uma chave Fernet válida para `SOCIAL_TOKEN_ENCRYPTION_KEY` quando necessário e a salva automaticamente no `.env`. Gere manualmente apenas um valor persistente para `FLASK_SECRET_KEY`:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

No [Google Cloud Console](https://console.cloud.google.com/), ative a **YouTube Data API v3**, crie credenciais OAuth para uma aplicação Web e adicione `http://localhost:5000/oauth/youtube/callback` como URI de redirecionamento autorizada. Preencha `YOUTUBE_CLIENT_ID` e `YOUTUBE_CLIENT_SECRET`. Consulte o [guia oficial de upload do YouTube](https://developers.google.com/youtube/v3/guides/uploading_a_video).

No [TikTok for Developers](https://developers.tiktok.com/), crie um app, habilite o **Content Posting API** e solicite aprovação para os escopos `user.info.basic,video.upload`. Cadastre `http://localhost:5000/oauth/tiktok/callback` como URI de redirecionamento e preencha `TIKTOK_CLIENT_KEY` e `TIKTOK_CLIENT_SECRET`. O login usa PKCE com `S256`; o painel mantém o `code_verifier` na sessão e o envia na troca do código. O TikTok recebe o vídeo como rascunho na caixa de entrada; a publicação deve ser concluída manualmente no app do TikTok. Consulte o [guia oficial de upload de rascunho](https://developers.tiktok.com/docs/en/content-posting-api-get-started-upload-content).

Para verificar o site no TikTok, copie o conteúdo exato fornecido pelo TikTok para `TIKTOK_DEVELOPERS_SITE_VERIFICATION_CODE` no `.env` e reinicie o painel. A rota `/tiktok-developers-site-verification.txt` o retorna como texto simples. Em produção, o domínio precisa estar publicamente acessível para a verificação. As páginas `/privacy` e `/terms` apresentam a Política de Privacidade e os Termos de Uso do painel.

Se o painel estiver em outro host ou porta, configure os dois `*_REDIRECT_URI` com as URLs públicas exatas cadastradas nos provedores. Para hospedagem HTTPS, defina `SESSION_COOKIE_SECURE=true` e mantenha uma `FLASK_SECRET_KEY` persistente.

Depois de reiniciar o painel, use **Conectar TikTok** e **Conectar YouTube** para autorizar as contas nas páginas oficiais. O botão do TikTok envia somente o vídeo à caixa de entrada como rascunho; não envia título, hashtags ou configurações de publicação. Abra a notificação no TikTok para editar e concluir a publicação. O YouTube envia o título e hashtags editados como vídeo privado. Se houver uma versão melhorada ainda disponível, ela é enviada; caso contrário, o painel baixa o original do Drive. Os tokens de acesso e renovação são criptografados com `SOCIAL_TOKEN_ENCRYPTION_KEY` em `social_tokens.enc`; o status por plataforma fica em `publicados.json`, e o corte só é marcado como postado quando a plataforma confirma a publicação. Se um envio iniciar mas não confirmar o resultado, o painel mantém o cartão bloqueado para evitar duplicatas; confira a conta antes de tentar novamente.

O YouTube Shorts usa o endpoint oficial de upload de vídeos do YouTube; a classificação como Short depende dos requisitos atuais de formato e duração do próprio YouTube. As plataformas e os painéis de desenvolvedor podem exigir aprovação, verificação do app e configuração adicional antes de autorizar ou publicar.

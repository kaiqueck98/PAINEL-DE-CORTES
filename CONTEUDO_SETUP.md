# Análise de conteúdo

O fluxo automático deixou de usar nomes de arquivos/pastas e modelos de título da
pasta. Cada vídeo usa três frames (10%, 50%, 90%) e transcrição Whisper local.
A integração multimodal recebe os frames e a transcrição, depois gera os textos
a partir da história detectada. Frames/transcrição são enviados à OpenAI, com
`store=false`; chamadas à API têm custo. Arquivos originais não são enviados à
API de análise. Nenhuma chamada paga é executada pelos testes.

No ambiente Python que executa o painel:

```powershell
python -m pip install -r requirements.txt
```

Configure no `.env` existente, sem publicar sua chave:

```dotenv
OPENAI_API_KEY=sua_chave
CONTENT_VISION_MODEL=gpt-4.1-mini
WHISPER_MODEL=base
AUTOPOST_TRENDS_FILE=C:/Users/kaiqueck/PAINEL DE CORTES/tendencias.json
```

Whisper baixa seu modelo na primeira transcrição; usa CPU. FFmpeg e FFprobe
precisam estar no PATH. A chave de Drive e as contas conectadas continuam sendo
necessárias. Não há título genérico de emergência: falhas são exibidas e o vídeo
não é aprovado.

## Lista de tendências

Crie o JSON indicado por `AUTOPOST_TRENDS_FILE` com:

- `source`: URL ou identificação da fonte de tendências realmente consultada;
- `updated_at`: data/hora dessa consulta, ISO 8601 com fuso;
- `hashtags`: lista de 1 a 100 strings começando com `#`.

A lista deve ter até 48 horas. Não basta renovar a data: consulte novamente a
fonte. O painel não verifica popularidade externamente e não fornece tendências
inventadas. O modelo só pode selecionar até cinco itens dessa lista, pertinentes
à história; pode retornar nenhum quando nada combinar. Fonte/data aparecem na tela.

## Seleção e execução

Ao ativar o agendamento e diariamente às 07:00 de São Paulo, todos os vídeos das
subpastas acessíveis são baixados temporariamente e analisados. Uma subpasta
inacessível impede tratar a listagem como completa. O ranking só fica pronto
depois de comparar o lote; o primeiro horário também tenta prepará-lo se necessário.

Nota de 0–100: resolução ≥720p (25), bitrate adequado (15), desfoque baixo (25),
duração de 15–60 segundos (25), áudio audível (10). Nomes, links e marcação manual
não pontuam. A nota é técnica, não uma previsão de viralização.

Até quatro candidatos distintos e com análise de conteúdo válida são escolhidos.
Com menos de quatro elegíveis, não se completa a lista com erros ou duplicatas.
Duplicatas exatas são detectadas por SHA-256; recodificações semelhantes por três
assinaturas visuais, duração próxima e transcrição igual quando há áudio. Isso
não garante identificar todo recorte, edição ou duplicata semântica.

O ranking, os textos e os motivos ficam no SQLite existente, em novas tabelas;
agendamentos anteriores e histórico são preservados. Arquivos temporários são
removidos. Os originais são baixados novamente para enviar, e o SHA-256 precisa
coincidir com o analisado. O fluxo anterior de melhoria automática continua ativo.

O servidor precisa permanecer ligado. Lotes grandes podem ultrapassar um horário;
o painel não publica o primeiro arquivo enquanto a comparação está incompleta.
O plano diário é estável: novos arquivos adicionados após a conclusão entram no
dia seguinte. Não remova as reservas de conteúdo para tentar repetir envios de
resultado incerto.

YouTube recebe título no campo de título e legenda + hashtags na descrição.
TikTok continua no envio para inbox/rascunho, sem título na API; use **Copiar título
e hashtags** na ficha. `payload_tiktok_publish` prepara o corpo do futuro Direct
Post com título + hashtags e exige `video.publish` e uma opção válida de privacidade.
`publicar_tiktok_direto` consulta creator_info, valida duração/privacidade,
inicializa Direct Post, transmite os blocos e confirma o status. Ele não altera
OAuth nem ativa Direct Post automaticamente: quando a permissão for aprovada,
a tela de autorização/exportação deve fornecer o novo escopo, a privacidade e o
consentimento. O fluxo atual continua sendo Sandbox/inbox.

Três frames não descrevem todo o vídeo. A história e a transcrição podem conter
erros; incertezas aparecem na ficha. Não há garantia de interpretação perfeita.

## Testes sem contas reais

```powershell
python -m unittest discover -s tests -v
```

Referências: [Whisper](https://github.com/openai/whisper),
[visão na Responses API](https://developers.openai.com/api/docs/guides/images-vision),
[TikTok Direct Post](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post).

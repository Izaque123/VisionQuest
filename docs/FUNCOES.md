# Funções do agente

Este texto descreve o Mark LVI neste computador: o que cada parte faz, como o controle de tela entra no programa, e o que sobrou no código sem participar do clique. O `readme.md` na raiz é o texto original do projeto, em inglês. Este arquivo é o mapa da instalação atual.

A entrada é `python main.py`.

## Como o programa sobe

1. `main.py` marca o processo como DPI por monitor antes do Qt. Sem isso o clique cai ao lado do alvo numa tela em 125%.
2. A janela (`ui.py`) abre o rosto, o microfone e os painéis.
3. `core/action_loader.py` lê cada `actions/*.py` que declara `TOOL` e registra a ferramenta. Na última execução foram 19.
4. `core/plugin_loader.py` lê cada `plugins/*.py` que declara `PLUGIN`. Hoje há um: `network_scanner`.
5. A voz conecta em `models/gemini-3.1-flash-live-preview`. Se a sessão cair, o laço em `JarvisLive.run` espera e conecta de novo.

Há duas formas de conversar. A padrão é o Gemini Live: o microfone vai direto para o modelo e a resposta volta em áudio. A outra, em `providers/`, junta reconhecimento de fala, um modelo de texto e uma voz. Ela só entra se for escolhida em ENGINE & PROVIDERS.

## A voz

`JarvisLive` em `main.py` mantém quatro tarefas ao mesmo tempo:

- `_listen_audio` lê o microfone. Não envia áudio enquanto o agente fala, enquanto está mudo, ou enquanto a cauda do próprio eco ainda está no ar.
- `_send_realtime` entrega esse áudio à sessão. O botão MUTAR esvazia a fila e manda um fim de fluxo, para a frase seguinte não continuar a conversa antiga.
- `_receive_audio` recebe áudio, transcrição e chamadas de ferramenta.
- `_play_audio` toca a resposta.

O botão de interromper e a tecla da pílula chamam `request_stop()` do controle de tela e também cortam o áudio que ainda ia sair.

Uma sessão Live tem duração máxima. Quando o servidor manda GoAway e a biblioteca não fecha o soquete, o corte é o erro 1008. O programa imprime uma linha, salva o resumo da conversa e reconecta.

## Três portas para uma ferramenta

O modelo não distingue a origem. Ele vê um nome e chama. A origem muda quem mantém o código.

**Dentro de `main.py`.** Ferramentas que precisam do estado da sessão ao vivo:

| Nome | Função |
|---|---|
| `screen_process` | Tira uma foto da tela ou da câmera e devolve a imagem para a mesma conversa de voz. Não clica. |
| `close_camera` | Fecha a câmera aberta por `screen_process`. |
| `system_status` | CPU, memória, GPU, temperatura. |
| `manage_monitor` | Liga ou desliga um assunto do acompanhamento diário de notícias. |
| `save_memory` / `recall_memory` | Grava e consulta a memória de longo prazo. |
| `undo` | Desfaz arquivo ou ajuste que o agente tenha registrado. |
| `shutdown_jarvis` | Encerra o assistente. |

**Arquivos em `actions/`.** Cada um traz o próprio `TOOL` e o handler. O núcleo não tem um `if` por ferramenta.

| Arquivo | Ferramenta | O que faz |
|---|---|---|
| `open_app.py` | `open_app` | Abre um programa e confere se o processo apareceu. |
| `desktop.py` | `desktop_control` | Janelas, atalhos e a área de trabalho por API, sem olhar a tela. |
| `computer_control.py` | `computer_control` | Tecla, clique informado, rolagem e fechar um aplicativo pelo nome. |
| `computer_settings.py` | `computer_settings` | Volume, brilho e ajustes do sistema. |
| `browser_control.py` | `browser_control` | Abre um endereço num navegador. Trocar de aba só vale para abas que esta ferramenta abriu. |
| `screen_drive.py` | `screen_drive` | Olha a tela deste computador e clica, digita ou rola. |
| `web_search.py` | `web_search` | Busca e notícias. Gemini primeiro; se a chave falha, DuckDuckGo. |
| `weather_report.py` | `weather_report` | Clima. |
| `youtube_video.py` | `youtube_video` | Acha um vídeo e abre no navegador do sistema. |
| `video_player.py` | `video_player` | Toca vídeo no próprio painel, começando mudo. |
| `file_controller.py` | `file_controller` | Cria, move, renomeia e apaga arquivos. |
| `file_processor.py` | `file_processor` | Lê documentos que já estão no disco. |
| `pdf_writer.py` | `create_pdf` | Gera um PDF. |
| `code_helper.py` | `code_helper` | Ajuda com código sem assumir a tela. |
| `dev_agent.py` | `dev_agent` | Tarefas de desenvolvimento em segundo plano. |
| `send_message.py` | `send_message` | Envia uma mensagem por um canal configurado. |
| `reminder.py` | `reminder` | Lembretes. |
| `flight_finder.py` | `flight_finder` | Procura voos. |
| `game_updater.py` | `game_updater` | Atualizações de jogos instalados. |

`actions/proactive.py`, `actions/background_monitor.py` e `actions/system_monitor.py` não são ferramentas. São laços de fundo: um pode puxar conversa depois de um silêncio, outro olha os assuntos de notícia uma vez por dia, o terceiro avisa se CPU ou temperatura passam do limite.

**Plugins.** Um arquivo em `plugins/` com `PLUGIN` e `run()`. Não precisa editar `main.py`. Entra desligado só se alguém desligar no painel PLUGINS; o padrão é ligado. `network_scanner` lista os aparelhos da rede privada deste PC. Ele recusa um endereço público.

## Controle de tela

`screen_drive` é funcionalidade do núcleo, no mesmo carregador das outras ações, com ganchos na janela.

O ciclo é local: uma foto deste monitor, uma decisão, um clique ou tecla neste computador. A foto vai para a escada `VISION` em `core/gemini.py`, com 12 segundos por modelo. A escada atual é `gemini-2.5-flash-lite`, `gemini-2.5-flash`, `gemini-3.1-flash-lite`, `gemini-3.5-flash`. Se nenhum devolver um JSON de ação, o ciclo para e não inventa um clique.

Há três mapas de coordenada, e eles não se misturam:

- Pixels físicos da captura. Nesta máquina, 1920×1080.
- A foto reduzida que o modelo vê, 1456×840, com a área desenhada em 1456×819 e uma faixa escura.
- O zoom, que é um recorte da foto original, não da foto reduzida.

O clique usa o desktop virtual do Windows. Com um monitor em (0, 0), o último pixel continua em 65535. A captura tenta DXGI e, se o quadro vier vazio, pede outro. Se continuar vazio, usa o mss. A lupa do Windows só entra se a foto normal vier preta.

Durante a tarefa a janela grande some. Ficam a borda, a pílula STOP e a miniatura com o olho e MUTAR/OUVIR. O ponteiro é o do Windows. F12 ou a pílula devolvem o mouse. O STOP também corta a espera da foto, para não ficar preso nos 12 segundos do modelo.

`screen_process` só mostra a imagem para a voz. `open_app` abre programas. `browser_control` abre endereços. Uma aba que já está visível é trabalho do `screen_drive`.

## Memória

`memory/long_term.json` guarda fatos entre sessões: identidade, preferências, projetos, relacionamentos, desejos, notas, até três resumos de sessão e os assuntos do monitor de notícias. A conversa aberta fica só na memória RAM, num identificador de retomada. Apagar o JSON zera os fatos. `config/api_keys.json` é a chave e as opções, e não faz parte dessa memória.

O modelo grava sozinho com `save_memory` quando ouve um fato pessoal. Relacionamentos são pessoas. Desejos são planos. Os dois podem estar vazios.

## Busca e chave

Voz e foto usam a mesma chave Gemini. Cota ou modelo indisponível na busca também tira esse modelo da foto. A busca de notícias, quando o Gemini não responde, depende de DuckDuckGo, Bing e outros. Se esses sites estouram o tempo, a notícia volta vazia mesmo com a voz conectada.

## Sujeira que ficou de propósito

Isto ainda está no código porque tirar mudaria uma função que funciona.

- `computer_settings.py` importa `comtypes` para o volume. Ele só entra nessa hora, não na captura da tela.
- `ui.py` importa `pynvml` para a porcentagem da GPU. O aviso no arranque não quebra essa leitura.
- O Qt avisa "Acesso negado" ao tentar marcar o DPI de novo. `main.py` já marcou. Tirar essa marcação faz o clique cair ao lado do alvo numa tela em 125%.
- O `readme.md` descreve o produto de origem, em inglês. Este arquivo descreve esta instalação.

## O que não é sujeira

- Pedidos de arquivo, código e PDF seguem pelas ações de segundo plano. Eles não movem o mouse.
- Música no painel é `video_player`. Abrir um vídeo no navegador é `youtube_video`. São ferramentas diferentes.
- A miniatura ao minimizar a janela usa o mesmo olho da tarefa de tela. Clicar no olho restaura a janela só quando não há uma tarefa de tela em curso.

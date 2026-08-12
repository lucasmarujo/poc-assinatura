"""Rotulador local: uma página com botões, servida por `validar.py rotular`.

Arrastar 400 arquivos no Explorer é o gargalo humano do processo inteiro, então
aqui a decisão é uma tecla: **S** tem assinatura, **N** não tem, **D** dúvida, e
a próxima imagem já está na tela. Cada clique grava na hora — não há botão de
salvar, não há trabalho para perder.

Um servidor `http.server` em `127.0.0.1` em vez de abrir o HTML direto do disco:
é o que permite gravar o rótulo no mesmo instante, sem `localStorage` para
sincronizar depois nem download para o usuário mover na mão. Só `stdlib`.

**Nada em `files/` é tocado, nem aqui nem em `amostra.py`.** A imagem de
`pendentes/` é a cópia renderizada; rotular apenas cria um vínculo dela dentro de
`com_assinatura/`, `sem_assinatura/` ou `duvida/` — `os.link` quando o sistema de
arquivos permite (mesmos bytes, zero espaço a mais), cópia quando não. O original
em `pendentes/` fica onde está.

A fila é embaralhada de propósito. Em ordem de índice, os ✅ e os ❌ sairiam em
blocos, e depois de dez seguidos o rotulador passa a adivinhar em vez de olhar —
a página não diz, e não pode deixar inferir, o que a tool decidiu.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from validacao.amostra import (
    NOME_AMOSTRA,
    PASTA_PENDENTES,
    PASTAS_ROTULO,
    TAMANHO_ID,
    ItemAmostra,
    localizar_imagem,
    pastas_de_rotulagem,
)

# O id vem da URL: é fronteira de confiança, e sem esta checagem um `..%2f`
# transformaria o rotulador em leitor de arquivo arbitrário da máquina.
_ID_VALIDO = re.compile(rf"^[0-9a-f]{{{TAMANHO_ID}}}$")

ROTULOS_VALIDOS: dict[str, bool | None] = {
    "sim": True,
    "nao": False,
    "duvida": None,
}
# Rótulo → pasta. O inverso de `PASTAS_ROTULO`, que é pasta → rótulo.
_PASTA_DO_ROTULO: dict[str, str] = {
    "sim": "com_assinatura",
    "nao": "sem_assinatura",
    "duvida": "duvida",
}

PORTA_DEFAULT = 8765
LIMITE_CORPO_BYTES = 4096
# Rota das imagens na página. Espelha o caminho em disco só para ficar óbvio de
# onde a imagem vem ao inspecionar a aba de rede.
ROTA_IMAGENS = f"{NOME_AMOSTRA}/{PASTA_PENDENTES}"


def fila(itens: list[ItemAmostra], pasta_amostra: Path, *, semente: Any = 42) -> list[ItemAmostra]:
    """Um item por conteúdo distinto que tem imagem, em ordem embaralhada.

    Determinística pela semente: reabrir o rotulador continua na mesma ordem, e
    o "faltam 120" de ontem continua sendo os mesmos 120.
    """
    por_conteudo = {item.sha256: item for item in itens}
    com_imagem = [
        item
        for item in sorted(por_conteudo.values(), key=lambda i: i.id)
        if localizar_imagem(pasta_amostra, item.id) is not None
    ]
    random.Random(f"{semente}:fila").shuffle(com_imagem)
    return com_imagem


def aplicar_rotulo(pasta_amostra: Path, identificador: str, rotulo: str) -> Path:
    """Vincula a imagem na pasta do rótulo e a retira das outras.

    Retirar das outras é o que impede um documento rotulado duas vezes de ficar
    com dois rótulos em disco — aí `ler_rotulos` passaria a depender da ordem de
    iteração das pastas para decidir qual vale.
    """
    if rotulo not in _PASTA_DO_ROTULO:
        raise ValueError(f"rótulo desconhecido: {rotulo!r}")
    if not _ID_VALIDO.match(identificador):
        raise ValueError(f"id inválido: {identificador!r}")

    pastas = pastas_de_rotulagem(pasta_amostra)
    origem = localizar_imagem(pasta_amostra, identificador)
    if origem is None:
        raise FileNotFoundError(f"sem imagem para `{identificador}`")

    destino = pastas[_PASTA_DO_ROTULO[rotulo]] / f"{identificador}.jpg"
    for nome in PASTAS_ROTULO:
        anterior = pastas[nome] / f"{identificador}.jpg"
        if anterior != destino and anterior.is_file():
            anterior.unlink()

    if not destino.exists():
        try:
            os.link(origem, destino)
        except OSError:
            # Volume sem hard link (FAT, rede): cópia mesmo, que é o pedido —
            # o original em `pendentes/` continua intacto de qualquer forma.
            shutil.copy2(origem, destino)
    return destino


def gravar_rotulo(caminho_jsonl: Path, sha256: str, rotulo: str) -> None:
    """Acrescenta o rótulo ao JSONL, com `flush`.

    Append, não reescrita: a última linha de um sha256 é a que vale (é assim que
    mudar de ideia funciona), e uma queda no meio da rotulagem não pode custar
    mais do que a decisão em curso.
    """
    caminho_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with caminho_jsonl.open("a", encoding="utf-8") as arquivo:
        arquivo.write(
            json.dumps(
                {"sha256": sha256, "tem_assinatura": ROTULOS_VALIDOS[rotulo]}, ensure_ascii=False
            )
            + "\n"
        )
        arquivo.flush()


def _bootstrap(
    itens: list[ItemAmostra], rotulos: dict[str, bool | None]
) -> str:
    """O JSON que a página recebe: a fila e o que já está decidido.

    Só `id` — nem caminho, nem estrato, nem o veredito da tool. A página não tem
    como revelar o que não recebeu.
    """
    por_valor = {True: "sim", False: "nao", None: "duvida"}
    return json.dumps(
        {
            "fila": [item.id for item in itens],
            "rotulos": {
                item.id: por_valor[rotulos[item.sha256]]
                for item in itens
                if item.sha256 in rotulos
            },
        },
        ensure_ascii=False,
    )


PAGINA = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rotular assinaturas</title>
<style>
  :root { color-scheme: dark; --fundo:#15171c; --painel:#1e2128; --borda:#31353f;
          --texto:#e8eaed; --fraco:#9aa0aa; --sim:#2f9e5f; --nao:#c0483c; --duvida:#8a7420; }
  * { box-sizing: border-box; }
  body { margin:0; height:100vh; display:flex; flex-direction:column;
         background:var(--fundo); color:var(--texto);
         font:14px/1.4 system-ui,Segoe UI,sans-serif; }
  header { display:flex; align-items:center; gap:16px; padding:10px 16px;
           background:var(--painel); border-bottom:1px solid var(--borda); }
  #barra { flex:1; height:6px; background:var(--borda); border-radius:3px; overflow:hidden; }
  #feito { height:100%; width:0; background:var(--sim); transition:width .15s; }
  #contagem { font-variant-numeric:tabular-nums; color:var(--fraco); white-space:nowrap; }
  #palco { flex:1; overflow:auto; display:flex; align-items:flex-start;
           justify-content:center; padding:12px; }
  #folha { background:#fff; box-shadow:0 2px 16px #0008; cursor:zoom-in; }
  #folha.zoom { cursor:zoom-out; }
  body:not(.ampliado) #folha { max-height:calc(100vh - 150px); width:auto; }
  body.ampliado #palco { align-items:flex-start; }
  footer { display:flex; gap:10px; align-items:center; justify-content:center;
           padding:12px 16px; background:var(--painel); border-top:1px solid var(--borda); }
  button { font:inherit; font-weight:600; color:var(--texto); border:1px solid var(--borda);
           background:#262a33; border-radius:8px; padding:11px 18px; cursor:pointer; }
  button:hover { filter:brightness(1.25); }
  button kbd { font:inherit; opacity:.55; margin-left:7px; }
  #sim { background:var(--sim); border-color:var(--sim); }
  #nao { background:var(--nao); border-color:var(--nao); }
  #duvida { background:var(--duvida); border-color:var(--duvida); }
  #atual { color:var(--fraco); min-width:9ch; text-align:center; }
  #fim { display:none; margin:auto; text-align:center; max-width:44ch; color:var(--fraco); }
  #fim strong { color:var(--texto); display:block; font-size:20px; margin-bottom:8px; }
  code { background:#0003; padding:2px 6px; border-radius:4px; }
</style>
<header>
  <strong>Rotular assinaturas</strong>
  <div id="barra"><div id="feito"></div></div>
  <span id="contagem"></span>
</header>
<div id="palco">
  <img id="folha" alt="documento">
  <div id="fim"><strong>Fila concluída.</strong>
    Rode <code>validar.py avaliar</code> para o relatório. Os rótulos já estão
    gravados em <code>auditoria/rotulos.jsonl</code>.</div>
</div>
<footer>
  <button id="voltar">&larr; Voltar</button>
  <button id="sim">Tem assinatura <kbd>S</kbd></button>
  <button id="nao">Não tem <kbd>N</kbd></button>
  <button id="duvida">Dúvida <kbd>D</kbd></button>
  <button id="pular">Pular &rarr;</button>
  <span id="atual"></span>
</footer>
<script>
const DADOS = __DADOS__;
const fila = DADOS.fila, rotulos = DADOS.rotulos;
const folha = document.getElementById('folha');
const fim = document.getElementById('fim');
let i = fila.findIndex(id => !(id in rotulos));
if (i < 0) i = fila.length;

function pintar() {
  const decididos = Object.keys(rotulos).length;
  document.getElementById('contagem').textContent =
    `${decididos} de ${fila.length} rotulados · faltam ${fila.length - decididos}`;
  document.getElementById('feito').style.width =
    (fila.length ? decididos / fila.length * 100 : 0) + '%';
  const acabou = i >= fila.length || i < 0;
  folha.style.display = acabou ? 'none' : '';
  fim.style.display = acabou ? 'block' : 'none';
  document.getElementById('atual').textContent = acabou ? '' :
    `${i + 1}/${fila.length}` + (fila[i] in rotulos ? ' · ' + rotulos[fila[i]] : '');
  if (!acabou) {
    folha.src = `amostra/pendentes/${fila[i]}.jpg`;
    // Adianta a próxima: sem isso, rotular em ritmo de teclado deixa a tela
    // branca entre um documento e o outro.
    if (i + 1 < fila.length) new Image().src = `amostra/pendentes/${fila[i + 1]}.jpg`;
  }
  document.body.classList.remove('ampliado');
  folha.classList.remove('zoom');
}

let ocupado = false;
async function rotular(rotulo) {
  // Trava de reentrada: duas teclas antes do fetch responder rotulariam o mesmo
  // documento duas vezes e pulariam o seguinte sem rótulo.
  if (ocupado || i < 0 || i >= fila.length) return;
  ocupado = true;
  const id = fila[i];
  try {
    const resposta = await fetch('api/rotulo', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, rotulo}),
    });
    if (!resposta.ok) { alert('Não consegui gravar: ' + await resposta.text()); return; }
    rotulos[id] = rotulo;
    i++;
    pintar();
  } finally {
    ocupado = false;
  }
}

document.getElementById('sim').onclick = () => rotular('sim');
document.getElementById('nao').onclick = () => rotular('nao');
document.getElementById('duvida').onclick = () => rotular('duvida');
document.getElementById('pular').onclick = () => { if (i < fila.length) { i++; pintar(); } };
document.getElementById('voltar').onclick = () => { if (i > 0) { i--; pintar(); } };
folha.onclick = () => {
  document.body.classList.toggle('ampliado');
  folha.classList.toggle('zoom');
};
document.addEventListener('keydown', evento => {
  const tecla = evento.key.toLowerCase();
  if (tecla === 's') rotular('sim');
  else if (tecla === 'n') rotular('nao');
  else if (tecla === 'd') rotular('duvida');
  else if (evento.key === 'ArrowLeft') { if (i > 0) { i--; pintar(); } }
  else if (evento.key === 'ArrowRight') { if (i < fila.length) { i++; pintar(); } }
  else return;
  evento.preventDefault();
});
pintar();
</script>
"""


def montar_pagina(itens: list[ItemAmostra], rotulos: dict[str, bool | None]) -> str:
    """A página com a fila embutida. `replace`, não `format`: o CSS/JS é cheio
    de `{}` e `str.format` os interpretaria como campo."""
    return PAGINA.replace("__DADOS__", _bootstrap(itens, rotulos))


def criar_servidor(
    *,
    itens: list[ItemAmostra],
    rotulos: dict[str, bool | None],
    pasta_amostra: Path,
    caminho_rotulos: Path,
    porta: int = PORTA_DEFAULT,
    ao_rotular: Callable[[str, str, int], None] | None = None,
) -> ThreadingHTTPServer:
    """Servidor do rotulador, preso a `127.0.0.1`.

    Só localhost: é ferramenta de mesa sobre documento de trabalhador, não pode
    ficar exposta na rede da máquina por descuido de bind.
    """
    pagina = montar_pagina(itens, rotulos).encode("utf-8")
    sha_por_id = {item.id: item.sha256 for item in itens}
    decididos = {item.id for item in itens if item.sha256 in rotulos}

    class Manipulador(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _responder(self, codigo: int, corpo: bytes, tipo: str) -> None:
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _erro(self, codigo: int, mensagem: str) -> None:
            # Fecha a conexão: em erro de POST o corpo pode não ter sido lido, e
            # em HTTP/1.1 com keep-alive o resto dele seria interpretado como a
            # requisição seguinte.
            self.close_connection = True
            self._responder(codigo, mensagem.encode("utf-8"), "text/plain; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802  (contrato do BaseHTTPRequestHandler)
            caminho = self.path.split("?", 1)[0]
            if caminho in ("/", "/index.html"):
                self._responder(HTTPStatus.OK, pagina, "text/html; charset=utf-8")
                return

            prefixo = f"/{ROTA_IMAGENS}/"
            if caminho.startswith(prefixo) and caminho.endswith(".jpg"):
                identificador = caminho[len(prefixo) : -len(".jpg")]
                if not _ID_VALIDO.match(identificador):
                    self._erro(HTTPStatus.BAD_REQUEST, "id inválido")
                    return
                imagem = localizar_imagem(pasta_amostra, identificador)
                if imagem is None:
                    self._erro(HTTPStatus.NOT_FOUND, "sem imagem")
                    return
                self._responder(HTTPStatus.OK, imagem.read_bytes(), "image/jpeg")
                return

            self._erro(HTTPStatus.NOT_FOUND, "não existe")

        def do_POST(self) -> None:  # noqa: N802  (contrato do BaseHTTPRequestHandler)
            if self.path.split("?", 1)[0] != "/api/rotulo":
                self._erro(HTTPStatus.NOT_FOUND, "não existe")
                return
            tamanho = int(self.headers.get("Content-Length") or 0)
            if tamanho <= 0 or tamanho > LIMITE_CORPO_BYTES:
                self._erro(HTTPStatus.BAD_REQUEST, "corpo ausente ou grande demais")
                return
            try:
                pedido = json.loads(self.rfile.read(tamanho).decode("utf-8"))
                identificador, rotulo = str(pedido["id"]), str(pedido["rotulo"])
            except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
                self._erro(HTTPStatus.BAD_REQUEST, "JSON inválido")
                return
            if rotulo not in ROTULOS_VALIDOS or identificador not in sha_por_id:
                self._erro(HTTPStatus.BAD_REQUEST, "id ou rótulo desconhecido")
                return

            try:
                # Pasta primeiro, JSONL depois: as pastas são a fonte que
                # `metricas.ler_rotulos` prefere, então não podem ficar atrás do
                # arquivo se algo falhar no meio.
                aplicar_rotulo(pasta_amostra, identificador, rotulo)
                gravar_rotulo(caminho_rotulos, sha_por_id[identificador], rotulo)
            except (OSError, ValueError) as exc:
                self._erro(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")
                return

            decididos.add(identificador)
            if ao_rotular is not None:
                ao_rotular(identificador, rotulo, len(decididos))
            self._responder(HTTPStatus.OK, b'{"ok":true}', "application/json")

        def log_message(self, formato: str, *args: Any) -> None:
            """Silencia o log de acesso: o progresso útil sai por `ao_rotular`."""

    return ThreadingHTTPServer(("127.0.0.1", porta), Manipulador)

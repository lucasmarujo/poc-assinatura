"""Triagem dos erros: a folha de contato ao lado do que a tool viu no documento.

O `VALIDACAO.md` diz *quantos* erros existem e em que estrato; ele não diz *qual*
foi o erro. Aqui cada divergência entre rótulo humano e veredito da tool vira um
cartão com a imagem que foi rotulada, o rótulo dado, o veredito e a evidência que
sustentou esse veredito — AcroForm `/Sig`, carimbo digital na camada de texto,
detecções do Nível 1, páginas em branco, fallback 3×3, truncamento.

É o que permite separar as três causas que somam a mesma taxa de erro e pedem
correções opostas: **modelo** (o detector errou), **rotulagem** (o humano errou,
ou a régua estava ambígua) e **documento** (arquivo corrompido, página que não
rasterizou, assinatura fora do que a folha de contato mostrou).

A causa escolhida na página fica no `localStorage` do navegador e sai por um
botão de download — nada é gravado em disco por aqui, e o `rotulos.jsonl` não é
tocado: mudar de ideia sobre um rótulo é `validar.py rotular`, não isto.

Só lê. Roda depois de `validar.py avaliar`, que é quem consolida as pastas de
rotulagem dentro do `rotulos.jsonl`.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Collection
from pathlib import Path
from typing import Any

from validacao.amostra import (
    DESCRICAO_ESTRATO,
    NOME_AMOSTRA,
    NOME_INDICE,
    NOME_ROTULOS,
    ItemAmostra,
    ler_indice,
    localizar_imagem,
)
from validacao.metricas import carregar_rotulos

NOME_ERROS = "ERROS.html"

# Causas da triagem que significam "o rótulo pode estar errado" — são estas que
# `validar.py rotular --rever` traz de volta para a fila. `modelo` e `documento`
# são o contrário: neles o rótulo foi dado por bom, e é a tool que errou.
CAUSAS_REVER = frozenset({"rever", "rotulagem"})

# Classe de cada cartão. `fp`/`fn` são os erros; os acertos entram para poder
# comparar um falso ❌ com um ✅ do mesmo estrato sem sair da página.
DESCRICAO_CLASSE: dict[str, str] = {
    "fn": "Falso ❌ — a tool disse que não tem, o rótulo diz que tem",
    "fp": "Falso ✅ — a tool disse que tem, o rótulo diz que não tem",
    "vp": "Acerto ✅ — as duas dizem que tem",
    "vn": "Acerto ❌ — as duas dizem que não tem",
    "duvida": "Dúvida — fora do denominador das taxas",
}

def _sim_nao(valor: Any) -> str:
    return "sim" if valor else "não"


def _fontes(registro: dict[str, Any]) -> list[str]:
    return [str(fonte) for fonte in (registro.get("fontes") or [])]


def detalhes(registro: dict[str, Any], *, copias: int) -> list[list[str]]:
    """As linhas do painel: o que a tool olhou e o que ela encontrou.

    Uma linha por evidência, e as do Nível 0 explícitas mesmo quando ausentes —
    "AcroForm: não" é a informação que separa o PDF assinado digitalmente do PDF
    escaneado com rubrica, e ela some se a linha só aparecer quando há sinal.
    """
    fontes = _fontes(registro)
    signatarios = [str(nome) for nome in (registro.get("signatarios") or [])]
    return [
        ["Arquivo", str(registro.get("arquivo", "—"))],
        [
            "Formato",
            f"{registro.get('formato', '—')} ({registro.get('tipo', '—')}) · "
            f"{copias} cópia(s) desse conteúdo no lote",
        ],
        ["Assinaturas contadas", f"{registro.get('assinaturas_total', 0)}"],
        [
            "Nível 0 — AcroForm `/Sig`",
            f"{_sim_nao('pdf_embedded' in fontes)} · {registro.get('nivel0_total', 0)} "
            "sinal(is) no Nível 0 (AcroForm + carimbo)",
        ],
        ["Nível 0 — carimbo digital no texto", _sim_nao("digital" in fontes)],
        ["Nível 1 — detecções visuais", f"{registro.get('nivel1_total', 0)}"],
        ["Signatários lidos", "; ".join(signatarios) if signatarios else "—"],
        [
            "Páginas",
            f"{registro.get('paginas', 0)} no documento · "
            f"{registro.get('paginas_analisadas_nivel1', 0)} analisadas no Nível 1 · "
            f"{registro.get('paginas_em_branco', 0)} em branco (não vão ao Nível 1)",
        ],
        [
            "Fallback 3×3",
            f"{registro.get('paginas_ladrilhadas', 0)} página(s) ladrilhada(s) — "
            "9 inferências por página",
        ],
        ["Truncado em `--max-paginas`", _sim_nao(registro.get("paginas_limitadas"))],
        ["Erro do Nível 1", str(registro.get("nivel1_erro") or "—")],
        ["Observação", str(registro.get("observacao") or "—")],
    ]


def _classe(veredito_tool: bool, rotulo: bool | None) -> str:
    if rotulo is None:
        return "duvida"
    if veredito_tool:
        return "vp" if rotulo else "fp"
    return "fn" if rotulo else "vn"


def coletar(
    *,
    registros: list[dict[str, Any]],
    indice: list[ItemAmostra],
    rotulos: dict[str, bool | None],
    pasta_saida: Path,
    raiz_files: Path,
) -> list[dict[str, Any]]:
    """Um cartão por conteúdo rotulado, com o registro do checkpoint anexado.

    Por conteúdo, não por documento: a folha de contato é a mesma para as N
    cópias, e o rótulo também — mostrar as N seria repetir a mesma imagem e a
    mesma decisão N vezes. Quantas cópias existem sai como campo.
    """
    por_arquivo = {str(registro.get("arquivo", "")): registro for registro in registros}
    copias: Counter[str] = Counter(item.sha256 for item in indice)
    por_conteudo: dict[str, list[ItemAmostra]] = defaultdict(list)
    for item in indice:
        por_conteudo[item.sha256].append(item)

    pasta_amostra = pasta_saida / NOME_AMOSTRA
    cartoes: list[dict[str, Any]] = []
    for sha256, itens in por_conteudo.items():
        if sha256 not in rotulos:
            continue
        # O registro pode faltar se `files/` mudou desde o sorteio; o primeiro
        # item ainda diz estrato, veredito e páginas, que é o essencial.
        item = next((i for i in itens if i.arquivo in por_arquivo), itens[0])
        registro = por_arquivo.get(item.arquivo, {"arquivo": item.arquivo})
        imagem = localizar_imagem(pasta_amostra, item.id)
        cartoes.append(
            {
                "id": item.id,
                "classe": _classe(item.veredito_tool, rotulos[sha256]),
                "estrato": item.estrato,
                "descricao_estrato": DESCRICAO_ESTRATO.get(item.estrato, item.estrato),
                "veredito": "assinado" if item.veredito_tool else "sem assinatura",
                "rotulo": {True: "tem assinatura", False: "não tem", None: "dúvida"}[
                    rotulos[sha256]
                ],
                "imagem": (
                    imagem.relative_to(pasta_saida).as_posix() if imagem is not None else None
                ),
                "original": _caminho_relativo(raiz_files / item.arquivo, pasta_saida),
                "detalhes": detalhes(registro, copias=copias[sha256]),
            }
        )
    # Erro primeiro: é o que se veio ver, e a lista tem 400+ cartões.
    ordem = {"fn": 0, "fp": 1, "duvida": 2, "vp": 3, "vn": 4}
    return sorted(cartoes, key=lambda cartao: (ordem[cartao["classe"]], cartao["estrato"]))


def ids_para_rever(
    caminho: Path, *, causas: Collection[str] = CAUSAS_REVER
) -> list[str]:
    """Ids do JSON baixado da triagem cuja causa pede um rótulo novo.

    Levanta `OSError`/`json.JSONDecodeError` — o arquivo é escolha explícita de
    quem chamou o comando, e apontar para o arquivo errado tem de aparecer, não
    virar uma fila vazia com cara de "nada a rever".
    """
    triagem = json.loads(caminho.read_text(encoding="utf-8"))
    return [
        str(item["id"])
        for item in triagem
        if isinstance(item, dict) and item.get("causa") in causas and item.get("id")
    ]


def _caminho_relativo(alvo: Path, base: Path) -> str:
    """Link para o documento original, relativo à pasta do HTML.

    Relativo e não `file://` absoluto: o caminho absoluto carrega o nome de
    usuário da máquina para dentro de um arquivo que circula por e-mail.
    """
    try:
        return Path(os.path.relpath(alvo, base)).as_posix()
    except ValueError:
        # Windows, unidades diferentes: sem caminho relativo possível.
        return alvo.as_posix()


PAGINA = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Triagem dos erros</title>
<style>
  :root { color-scheme: dark; --fundo:#15171c; --painel:#1e2128; --borda:#31353f;
          --texto:#e8eaed; --fraco:#9aa0aa; --sim:#2f9e5f; --nao:#c0483c; --duvida:#8a7420;
          --modelo:#7a5cc0; --rotulagem:#2f7fb0; --documento:#a9702a; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--fundo); color:var(--texto);
         font:14px/1.5 system-ui,Segoe UI,sans-serif; }
  header { position:sticky; top:0; z-index:2; display:flex; flex-wrap:wrap; gap:10px;
           align-items:center; padding:10px 16px; background:var(--painel);
           border-bottom:1px solid var(--borda); }
  header strong { margin-right:6px; }
  select, button { font:inherit; color:var(--texto); background:#262a33;
                   border:1px solid var(--borda); border-radius:8px; padding:7px 12px;
                   cursor:pointer; }
  button:hover, select:hover { filter:brightness(1.25); }
  button.ativo { border-color:var(--texto); background:#333844; }
  #contagem { color:var(--fraco); font-variant-numeric:tabular-nums; margin-left:auto; }
  main { display:flex; flex-direction:column; gap:14px; padding:14px; }
  article { display:grid; grid-template-columns:minmax(280px,42%) 1fr; gap:14px;
            background:var(--painel); border:1px solid var(--borda); border-radius:10px;
            overflow:hidden; }
  @media (max-width:900px) { article { grid-template-columns:1fr; } }
  .folha { background:#fff; width:100%; display:block; cursor:zoom-in; max-height:460px;
           object-fit:contain; object-position:top; }
  .folha.zoom { max-height:none; cursor:zoom-out; }
  .sem-imagem { display:flex; align-items:center; justify-content:center; color:var(--fraco);
                background:#0003; min-height:180px; }
  .painel { padding:12px 14px 14px 0; }
  .titulo { font-weight:600; margin-bottom:2px; }
  .fn .titulo, .vn .titulo { color:var(--nao); }
  .fp .titulo, .vp .titulo { color:var(--sim); }
  .duvida .titulo { color:var(--duvida); }
  .vp .titulo, .vn .titulo { color:var(--fraco); }
  .estrato { color:var(--fraco); margin-bottom:10px; }
  .veredito { display:flex; gap:18px; flex-wrap:wrap; margin-bottom:10px; }
  .veredito div { background:#0003; border-radius:8px; padding:6px 10px; }
  .veredito span { color:var(--fraco); display:block; font-size:12px; }
  table { border-collapse:collapse; width:100%; }
  td { border-top:1px solid var(--borda); padding:4px 8px 4px 0; vertical-align:top; }
  td:first-child { color:var(--fraco); white-space:nowrap; width:1%; }
  .causas { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; align-items:center; }
  .causas span { color:var(--fraco); }
  .causas button[data-causa="modelo"].ativo { background:var(--modelo); }
  .causas button[data-causa="rotulagem"].ativo { background:var(--rotulagem); }
  .causas button[data-causa="documento"].ativo { background:var(--documento); }
  a { color:#78b7ff; }
  code { background:#0003; padding:1px 5px; border-radius:4px; }
  #vazio { color:var(--fraco); padding:24px; text-align:center; }
</style>
<header>
  <strong>Triagem dos erros</strong>
  <button data-filtro="erros" class="ativo">Só erros</button>
  <button data-filtro="fn">Falso ❌</button>
  <button data-filtro="fp">Falso ✅</button>
  <button data-filtro="duvida">Dúvida</button>
  <button data-filtro="tudo">Tudo</button>
  <select id="estrato"></select>
  <button id="baixar">Baixar triagem (JSON)</button>
  <span id="contagem"></span>
</header>
<main id="lista"></main>
<div id="vazio" hidden>Nada nesse filtro.</div>
<script>
const DADOS = __DADOS__;
const CHAVE = 'triagem-erros';
const lista = document.getElementById('lista');
const seletor = document.getElementById('estrato');
let filtro = 'erros';

// localStorage e não arquivo: a página é estática, aberta com duplo clique.
// Falha (modo privado, file:// restrito) não pode derrubar a visualização, que
// é o que realmente importa aqui.
function lerCausas() {
  try { return JSON.parse(localStorage.getItem(CHAVE) || '{}'); } catch { return {}; }
}
function gravarCausas(causas) {
  try { localStorage.setItem(CHAVE, JSON.stringify(causas)); } catch {}
}
const causas = lerCausas();

const estratos = [...new Set(DADOS.map(c => c.estrato))].sort();
seletor.innerHTML = ['<option value="">todos os estratos</option>']
  .concat(estratos.map(e => `<option value="${e}">${e}</option>`)).join('');

function visiveis() {
  const estrato = seletor.value;
  return DADOS.filter(c =>
    (!estrato || c.estrato === estrato) &&
    (filtro === 'tudo' ||
     (filtro === 'erros' ? (c.classe === 'fn' || c.classe === 'fp') : c.classe === filtro)));
}

function escapar(texto) {
  return String(texto).replace(/[&<>"]/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[s]));
}

// O `texto` dos detalhes vem com `crase` do vocabulário do projeto (`/Sig`,
// `--max-paginas`): vira <code> depois de escapado, nunca antes.
function marcar(texto) {
  return escapar(texto).replace(/`([^`]+)`/g, '<code>$1</code>');
}

function cartao(c) {
  const imagem = c.imagem
    ? `<img class="folha" src="${escapar(c.imagem)}" alt="documento ${escapar(c.id)}" loading="lazy">`
    : '<div class="sem-imagem">sem imagem renderizada</div>';
  const linhas = c.detalhes
    .map(([rotulo, valor]) => `<tr><td>${marcar(rotulo)}</td><td>${marcar(valor)}</td></tr>`)
    .join('');
  const botoes = ['modelo', 'rotulagem', 'documento', 'rever']
    .map(causa => `<button data-causa="${causa}" data-id="${c.id}"` +
                  `${causas[c.id] === causa ? ' class="ativo"' : ''}>${causa}</button>`)
    .join('');
  return `<article class="${c.classe}" data-id="${c.id}">
    <div>${imagem}</div>
    <div class="painel">
      <div class="titulo">${escapar(DADOS.legendas[c.classe] || c.classe)}</div>
      <div class="estrato">${marcar(c.descricao_estrato)}</div>
      <div class="veredito">
        <div><span>a tool decidiu</span>${escapar(c.veredito)}</div>
        <div><span>o rótulo humano diz</span>${escapar(c.rotulo)}</div>
        <div><span>conteúdo</span><code>${escapar(c.id)}</code></div>
        <div><span>original</span><a href="${escapar(c.original)}">abrir arquivo</a></div>
      </div>
      <table>${linhas}</table>
      <div class="causas"><span>causa do erro:</span>${botoes}</div>
    </div>
  </article>`;
}

function pintar() {
  const itens = visiveis();
  lista.innerHTML = itens.map(cartao).join('');
  document.getElementById('vazio').hidden = itens.length > 0;
  const decididas = itens.filter(c => causas[c.id]).length;
  document.getElementById('contagem').textContent =
    `${itens.length} cartão(ões) · ${decididas} com causa marcada · ${DADOS.length} rotulados no total`;
}

document.querySelectorAll('header button[data-filtro]').forEach(botao => {
  botao.onclick = () => {
    filtro = botao.dataset.filtro;
    document.querySelectorAll('header button[data-filtro]')
      .forEach(b => b.classList.toggle('ativo', b === botao));
    pintar();
  };
});
seletor.onchange = pintar;

lista.onclick = evento => {
  const alvo = evento.target;
  if (alvo.classList.contains('folha')) { alvo.classList.toggle('zoom'); return; }
  if (!alvo.dataset || !alvo.dataset.causa) return;
  const id = alvo.dataset.id;
  causas[id] = causas[id] === alvo.dataset.causa ? undefined : alvo.dataset.causa;
  if (!causas[id]) delete causas[id];
  gravarCausas(causas);
  alvo.parentElement.querySelectorAll('button')
    .forEach(b => b.classList.toggle('ativo', causas[id] === b.dataset.causa));
  pintar();
};

document.getElementById('baixar').onclick = () => {
  const saida = DADOS
    .filter(c => causas[c.id])
    .map(c => ({id: c.id, classe: c.classe, estrato: c.estrato, causa: causas[c.id]}));
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(saida, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url; link.download = 'triagem-erros.json'; link.click();
  URL.revokeObjectURL(url);
};

pintar();
</script>
"""


def montar_pagina(cartoes: list[dict[str, Any]]) -> str:
    """A página com os cartões embutidos. `replace`, não `format`: o CSS/JS é
    cheio de `{}` e `str.format` os interpretaria como campo."""
    dados = json.dumps(cartoes, ensure_ascii=False)
    legendas = json.dumps(DESCRICAO_CLASSE, ensure_ascii=False)
    # A legenda viaja pendurada no array para o JS ter os dois numa variável só.
    embutido = f"Object.assign({dados}, {{legendas: {legendas}}})"
    return PAGINA.replace("__DADOS__", embutido)


def gerar(
    *,
    registros: list[dict[str, Any]],
    pasta_saida: Path,
    raiz_files: Path,
) -> tuple[Path, Counter[str]]:
    """Escreve o `ERROS.html` e devolve o caminho e a contagem por classe."""
    indice = ler_indice(pasta_saida / NOME_INDICE)
    rotulos = carregar_rotulos(pasta_saida / NOME_ROTULOS)
    cartoes = coletar(
        registros=registros,
        indice=indice,
        rotulos=rotulos,
        pasta_saida=pasta_saida,
        raiz_files=raiz_files,
    )
    pasta_saida.mkdir(parents=True, exist_ok=True)
    caminho = pasta_saida / NOME_ERROS
    caminho.write_text(montar_pagina(cartoes), encoding="utf-8")
    return caminho, Counter(cartao["classe"] for cartao in cartoes)

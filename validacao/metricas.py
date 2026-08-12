"""Matriz de confusão, taxas de acerto e o `VALIDACAO.md`.

O que se mede é o veredito binário por documento — ✅ contra ❌ —, não a caixa
que o Nível 1 desenhou: é essa a decisão que a tool entrega, e é dela que o
rótulo humano fala. Os ⚠️ ficam fora e voltam como cobertura.

**Reponderação.** A amostra sobrerrepresenta de propósito os estratos de risco
(o fallback 3×3, o documento truncado). Cada estrato tem então as células da sua
amostra multiplicadas por `N_estrato / n_rotulado_no_estrato` antes de somar, o
que reconstrói a matriz de confusão do lote inteiro e desfaz o oversampling. Sem
isso, concentrar esforço onde o erro está pioraria artificialmente o número
global.

**Intervalo de confiança.** Wilson 95%, dimensionado pelo tamanho *bruto* da
amostra do denominador de cada taxa e centrado na estimativa ponderada. É
aproximação — o desenho estratificado com pesos desiguais tem variância um pouco
diferente —, mas é honesta na ordem de grandeza, é a que responde "isto está em
95% ou em 70%", e não traz o scipy para dentro do projeto por causa dela.

As cinco taxas, com o nome da pergunta que cada uma responde:

| Taxa | Pergunta |
|---|---|
| Acurácia | de tudo que a tool decidiu, quanto está certo |
| Precisão | quando disse ✅, realmente tinha assinatura |
| Recall | dos documentos que tinham assinatura, quantos ela achou |
| Especificidade | dos que não tinham, quantos ela deixou como ❌ |
| **VPN** | **quando disse ❌, realmente não tinha** |
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from validacao.amostra import (
    DESCRICAO_ESTRATO,
    NOME_AMOSTRA,
    NOME_INDICE,
    NOME_ROTULOS,
    PASTAS_ROTULO,
    TAMANHO_ID,
    ItemAmostra,
    estrato,
    ler_indice,
)

Z_95 = 1.959964

# Acima disto, a rotulagem não decidiu o suficiente para a métrica valer: ou a
# folha de contato está ruim, ou a régua do que conta como assinatura está
# ambígua. Nos dois casos o número sai com ressalva no relatório.
LIMITE_DUVIDA_ACEITAVEL = 0.05

NOMES_TAXAS: dict[str, str] = {
    "acuracia": "Acurácia — de tudo que decidiu, quanto acertou",
    "precisao": "Precisão — quando disse ✅, tinha assinatura",
    "recall": "Recall — das que tinham, quantas achou",
    "especificidade": "Especificidade — das que não tinham, quantas deixou ❌",
    "vpn": "VPN — quando disse ❌, não tinha mesmo",
}


def wilson(sucessos: float, total: float, *, z: float = Z_95) -> tuple[float, float]:
    """Intervalo de Wilson para uma proporção (só `math`, sem scipy).

    Preferido ao intervalo normal simples porque não estoura de [0,1] nem colapsa
    para largura zero quando a proporção encosta em 100% — que é o regime
    esperado aqui.
    """
    if total <= 0:
        return (0.0, 0.0)
    proporcao = sucessos / total
    denominador = 1 + z * z / total
    centro = (proporcao + z * z / (2 * total)) / denominador
    margem = (
        z * math.sqrt(proporcao * (1 - proporcao) / total + z * z / (4 * total * total))
    ) / denominador
    return (max(0.0, centro - margem), min(1.0, centro + margem))


@dataclass(frozen=True)
class Celulas:
    """Matriz de confusão. `vp` = disse ✅ e tinha; `fn` = disse ❌ e tinha."""

    vp: float = 0.0
    fp: float = 0.0
    vn: float = 0.0
    fn: float = 0.0

    @property
    def total(self) -> float:
        return self.vp + self.fp + self.vn + self.fn

    @property
    def acertos(self) -> float:
        return self.vp + self.vn

    @property
    def erros(self) -> float:
        return self.fp + self.fn

    def __add__(self, outra: Celulas) -> Celulas:
        return Celulas(
            vp=self.vp + outra.vp,
            fp=self.fp + outra.fp,
            vn=self.vn + outra.vn,
            fn=self.fn + outra.fn,
        )

    def escalar(self, fator: float) -> Celulas:
        return Celulas(
            vp=self.vp * fator, fp=self.fp * fator, vn=self.vn * fator, fn=self.fn * fator
        )

    def para_dict(self) -> dict[str, float]:
        return {
            "verdadeiro_positivo": round(self.vp, 1),
            "falso_positivo": round(self.fp, 1),
            "verdadeiro_negativo": round(self.vn, 1),
            "falso_negativo": round(self.fn, 1),
        }


def celula(veredito_tool: bool, rotulo: bool) -> Celulas:
    """Um par (veredito, rótulo) na célula certa da matriz."""
    if veredito_tool:
        return Celulas(vp=1.0) if rotulo else Celulas(fp=1.0)
    return Celulas(fn=1.0) if rotulo else Celulas(vn=1.0)


# ---------- rótulos -----------------------------------------------------------


def ler_rotulos(pasta_amostra: Path, *, conhecidos: dict[str, str]) -> dict[str, bool | None]:
    """Rótulos lidos das pastas: `sha256 → True | False | None (dúvida)`.

    A pasta em que a imagem está **é** o rótulo — não há arquivo de estado para
    dessincronizar. `conhecidos` traduz o nome curto da imagem de volta para o
    sha256 completo; imagem cujo id não é conhecido (sobrou de uma amostra com
    outra semente) é ignorada.
    """
    rotulos: dict[str, bool | None] = {}
    for nome, valor in PASTAS_ROTULO.items():
        pasta = pasta_amostra / nome
        if not pasta.is_dir():
            continue
        for imagem in pasta.glob("*.jpg"):
            sha256 = conhecidos.get(imagem.stem)
            if sha256:
                rotulos[sha256] = valor
    return rotulos


def carregar_rotulos(caminho: Path) -> dict[str, bool | None]:
    """Rótulos já persistidos. Chaveado por sha256, sem caminho de arquivo —
    o que o torna versionável (não carrega nome de trabalhador) e imune a
    reorganização de `files/`."""
    if not caminho.is_file():
        return {}
    rotulos: dict[str, bool | None] = {}
    with caminho.open(encoding="utf-8") as arquivo:
        for linha in arquivo:
            linha = linha.strip()
            if not linha:
                continue
            try:
                registro = json.loads(linha)
                rotulos[registro["sha256"]] = registro["tem_assinatura"]
            except (json.JSONDecodeError, KeyError):
                continue
    return rotulos


def salvar_rotulos(caminho: Path, rotulos: dict[str, bool | None]) -> None:
    """Reescreve o arquivo de rótulos (as pastas são a fonte da verdade)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8") as arquivo:
        for sha256 in sorted(rotulos):
            arquivo.write(
                json.dumps(
                    {"sha256": sha256, "tem_assinatura": rotulos[sha256]}, ensure_ascii=False
                )
                + "\n"
            )


# ---------- avaliação ---------------------------------------------------------


def _indicador(
    numerador: float, denominador: float, n_bruto: float
) -> dict[str, Any]:
    """Uma taxa com o IC de Wilson, ou `None` quando não há denominador."""
    if denominador <= 0 or n_bruto <= 0:
        return {"valor": None, "ic": None, "n": int(n_bruto)}
    proporcao = numerador / denominador
    baixo, alto = wilson(proporcao * n_bruto, n_bruto)
    return {"valor": proporcao, "ic": [baixo, alto], "n": int(n_bruto)}


def taxas(estimadas: Celulas, brutas: Celulas) -> dict[str, Any]:
    """As cinco taxas + F1. Valor da matriz ponderada, IC do tamanho bruto."""
    resultado = {
        "acuracia": _indicador(estimadas.acertos, estimadas.total, brutas.total),
        "precisao": _indicador(estimadas.vp, estimadas.vp + estimadas.fp, brutas.vp + brutas.fp),
        "recall": _indicador(estimadas.vp, estimadas.vp + estimadas.fn, brutas.vp + brutas.fn),
        "especificidade": _indicador(
            estimadas.vn, estimadas.vn + estimadas.fp, brutas.vn + brutas.fp
        ),
        "vpn": _indicador(estimadas.vn, estimadas.vn + estimadas.fn, brutas.vn + brutas.fn),
    }
    precisao, recall = resultado["precisao"]["valor"], resultado["recall"]["valor"]
    resultado["f1"] = {
        "valor": (
            2 * precisao * recall / (precisao + recall)
            if precisao and recall and precisao + recall
            else None
        ),
        "ic": None,
        "n": resultado["acuracia"]["n"],
    }
    return resultado


_ORDEM_ESTRATOS = list(DESCRICAO_ESTRATO)


def _ordem_do_estrato(par: tuple[str, Any]) -> int:
    """Ordem de `DESCRICAO_ESTRATO` no relatório; desconhecido vai para o fim."""
    return _ORDEM_ESTRATOS.index(par[0]) if par[0] in _ORDEM_ESTRATOS else len(_ORDEM_ESTRATOS)


def avaliar(
    *,
    registros: list[dict[str, Any]],
    hashes: dict[str, str],
    indice: list[ItemAmostra],
    rotulos: dict[str, bool | None],
) -> dict[str, Any]:
    """Cruza rótulo humano × veredito da tool e devolve tudo que o relatório usa.

    O `dict` devolvido é também o `VALIDACAO.json`: quem quiser comparar duas
    execuções (mudou `--conf`, trocou o modelo) compara esse arquivo.
    """
    populacao = Counter(nome for registro in registros if (nome := estrato(registro)))
    avaliaveis = sum(populacao.values())

    por_estrato: dict[str, dict[str, Any]] = {}
    for item in indice:
        dados = por_estrato.setdefault(
            item.estrato,
            {
                "populacao": populacao.get(item.estrato, 0),
                "sorteados": 0,
                "rotulados": 0,
                "duvidas": 0,
                "celulas": Celulas(),
            },
        )
        dados["sorteados"] += 1
        if item.sha256 not in rotulos:
            continue
        rotulo = rotulos[item.sha256]
        if rotulo is None:
            dados["duvidas"] += 1
            continue
        dados["rotulados"] += 1
        dados["celulas"] = dados["celulas"] + celula(item.veredito_tool, rotulo)

    brutas = Celulas()
    estimadas = Celulas()
    for dados in por_estrato.values():
        brutas = brutas + dados["celulas"]
        if dados["rotulados"] and dados["populacao"]:
            fator = dados["populacao"] / dados["rotulados"]
            dados["fator"] = round(fator, 2)
            dados["erros_estimados"] = round(dados["celulas"].erros * fator, 1)
            estimadas = estimadas + dados["celulas"].escalar(fator)
        else:
            dados["fator"] = None
            dados["erros_estimados"] = 0.0

    # Cobertura por rótulo direto: documento cujo conteúdo foi rotulado à mão
    # está verificado, não estimado. É o ganho da deduplicação aparecendo.
    rotulados_decididos = {sha for sha, valor in rotulos.items() if valor is not None}
    com_rotulo_direto = sum(
        1
        for registro in registros
        if estrato(registro) and hashes.get(registro.get("arquivo", "")) in rotulados_decididos
    )

    # Estrato sem nenhum rótulo não entra na matriz estimada — então a matriz
    # descreve só a parte do lote que ele cobre. Isso tem de sair no relatório,
    # não ficar implícito num total que não fecha com o lote.
    populacao_representada = sum(
        dados["populacao"] for dados in por_estrato.values() if dados["rotulados"]
    )

    duvidas = sum(dados["duvidas"] for dados in por_estrato.values())
    decididos = int(brutas.total)
    return {
        "amostra": {
            "sorteados": sum(dados["sorteados"] for dados in por_estrato.values()),
            "conteudos_distintos": len({item.sha256 for item in indice}),
            "rotulados": decididos,
            "duvidas": duvidas,
            # Em imagens, não em documentos: é este o número que diz quanto
            # trabalho humano falta.
            "pendentes": len({item.sha256 for item in indice} - set(rotulos)),
            "fracao_duvida": (
                round(duvidas / (decididos + duvidas), 4) if decididos + duvidas else 0.0
            ),
        },
        "lote": {
            "documentos_avaliaveis": avaliaveis,
            "documentos": len(registros),
            "documentos_com_rotulo_direto": com_rotulo_direto,
            "conteudos_distintos_rotulados": len(rotulados_decididos),
            "populacao_representada": populacao_representada,
            "fracao_representada": (
                round(populacao_representada / avaliaveis, 4) if avaliaveis else 0.0
            ),
        },
        "matriz_estimada": estimadas.para_dict(),
        "matriz_amostra": brutas.para_dict(),
        "taxas": taxas(estimadas, brutas),
        "por_estrato": {
            nome: {
                "descricao": DESCRICAO_ESTRATO.get(nome, nome),
                "populacao": dados["populacao"],
                "sorteados": dados["sorteados"],
                "rotulados": dados["rotulados"],
                "duvidas": dados["duvidas"],
                "fator": dados["fator"],
                "erros_na_amostra": int(dados["celulas"].erros),
                "erros_estimados": dados["erros_estimados"],
                "matriz": dados["celulas"].para_dict(),
            }
            for nome, dados in sorted(por_estrato.items(), key=_ordem_do_estrato)
        },
    }


# ---------- relatório ---------------------------------------------------------


def _pct(valor: float | None) -> str:
    return "—" if valor is None else f"{valor:.1%}"


def _linha_taxa(chave: str, indicador: dict[str, Any]) -> str:
    ic = indicador["ic"]
    faixa = "—" if ic is None else f"{ic[0]:.1%} – {ic[1]:.1%}"
    return (
        f"| {NOMES_TAXAS.get(chave, chave)} | **{_pct(indicador['valor'])}** "
        f"| {faixa} | {indicador['n']} |"
    )


def montar_validacao(dados: dict[str, Any], *, execucao: dict[str, Any]) -> str:
    """`VALIDACAO.md`: o número, o intervalo, e onde o erro está."""
    amostra, lote = dados["amostra"], dados["lote"]
    estimada, bruta = dados["matriz_estimada"], dados["matriz_amostra"]

    linhas = [
        "# Validação — acurácia real da detecção de assinatura",
        "",
        f"Amostra de **{amostra['sorteados']}** documentos "
        f"({amostra['conteudos_distintos']} conteúdos distintos) sorteada com semente "
        f"`{execucao.get('semente', '?')}` sobre um lote de "
        f"{lote['documentos_avaliaveis']} documentos avaliáveis (✅ + ❌).",
        "",
        f"Documentos rotulados: **{amostra['rotulados']}** · dúvida: "
        f"{amostra['duvidas']} · imagens ainda em `pendentes/`: "
        f"{amostra['pendentes']}.",
        "",
    ]

    if not amostra["rotulados"]:
        linhas += [
            "> Nenhum rótulo ainda. Mova as imagens de "
            f"`{NOME_AMOSTRA}/pendentes/` para `com_assinatura/`, `sem_assinatura/` "
            "ou `duvida/` e rode `validar.py avaliar` de novo.",
            "",
        ]
        return "\n".join(linhas) + "\n"

    if amostra["fracao_duvida"] > LIMITE_DUVIDA_ACEITAVEL:
        linhas += [
            f"> ⚠️ {amostra['fracao_duvida']:.1%} da amostra ficou em **dúvida** "
            f"(acima de {LIMITE_DUVIDA_ACEITAVEL:.0%}). As taxas abaixo saem sobre o que "
            "foi decidido, mas dúvida nesse volume costuma indicar régua ambígua ou "
            "folha de contato com resolução insuficiente — vale revisar antes de citar "
            "o número.",
            "",
        ]

    if lote["fracao_representada"] < 1.0:
        faltando = lote["documentos_avaliaveis"] - lote["populacao_representada"]
        linhas += [
            f"> ⚠️ As taxas cobrem {lote['fracao_representada']:.1%} do lote: "
            f"{faltando} documento(s) estão em estratos que ainda não têm nenhum "
            "rótulo decidido, e um estrato sem rótulo não pode ser reponderado. "
            "Rotule ao menos um documento de cada estrato da tabela abaixo para o "
            "número passar a valer para o lote inteiro.",
            "",
        ]

    acertos_amostra = bruta["verdadeiro_positivo"] + bruta["verdadeiro_negativo"]
    linhas += [
        "## Veredito",
        "",
        "**Estimativa** porque foram rotulados "
        f"{amostra['rotulados']} documentos, não os {lote['documentos_avaliaveis']} do "
        "lote: a coluna projeta a amostra para o lote inteiro, e o IC é a incerteza "
        "dessa projeção. O ponto também não é a média crua da amostra — cada estrato "
        "entra multiplicado por `N_estrato / rotulados_no_estrato`, senão o "
        "oversampling dos estratos de risco pioraria o total artificialmente.",
        "",
        f"Medido na amostra, sem projeção: **{acertos_amostra:.0f} acertos em "
        f"{amostra['rotulados']}** "
        f"({acertos_amostra / max(amostra['rotulados'], 1):.1%}).",
        "",
        "| Taxa | Estimativa p/ o lote | IC 95% | n da amostra |",
        "|---|---|---|---|",
        *(_linha_taxa(chave, dados["taxas"][chave]) for chave in NOMES_TAXAS),
        f"| F1 | **{_pct(dados['taxas']['f1']['valor'])}** | — | "
        f"{dados['taxas']['f1']['n']} |",
        "",
        "## Matriz de confusão",
        "",
        "Estimada para o lote inteiro (amostra de cada estrato reponderada pelo "
        "tamanho do estrato) e, ao lado, a contagem crua da amostra:",
        "",
        "| | Tinha assinatura | Não tinha |",
        "|---|---|---|",
        f"| Tool disse ✅ | {estimada['verdadeiro_positivo']:,.0f} "
        f"(amostra {bruta['verdadeiro_positivo']:.0f}) "
        f"| {estimada['falso_positivo']:,.0f} "
        f"(amostra {bruta['falso_positivo']:.0f}) |",
        f"| Tool disse ❌ | {estimada['falso_negativo']:,.0f} "
        f"(amostra {bruta['falso_negativo']:.0f}) "
        f"| {estimada['verdadeiro_negativo']:,.0f} "
        f"(amostra {bruta['verdadeiro_negativo']:.0f}) |",
        "",
        f"Falso ✅ aprova documento sem assinatura; falso ❌ manda para revisão "
        f"manual algo que já estava assinado. A estimativa é de "
        f"**{estimada['falso_positivo']:,.0f}** e **{estimada['falso_negativo']:,.0f}** "
        "documentos, respectivamente, no lote inteiro.",
        "",
        "## Onde o erro está",
        "",
        "| Estrato | No lote | Rotulados | Erros na amostra | Erros estimados no lote |",
        "|---|---|---|---|---|",
    ]

    total_erros = sum(item["erros_estimados"] for item in dados["por_estrato"].values()) or 1.0
    for nome, item in dados["por_estrato"].items():
        taxa_amostra = (
            f"{item['erros_na_amostra']}/{item['rotulados']}" if item["rotulados"] else "—"
        )
        linhas.append(
            f"| {item['descricao']} | {item['populacao']} | {item['rotulados']} "
            f"| {taxa_amostra} | {item['erros_estimados']:,.0f} "
            f"({item['erros_estimados'] / total_erros:.0%}) |"
        )

    linhas += [
        "",
        "## Cobertura",
        "",
        f"- **{lote['documentos_com_rotulo_direto']}** dos "
        f"{lote['documentos_avaliaveis']} documentos avaliáveis "
        f"({lote['documentos_com_rotulo_direto'] / max(lote['documentos_avaliaveis'], 1):.1%}) "
        f"têm rótulo humano **direto** — são {lote['conteudos_distintos_rotulados']} "
        "conteúdos distintos rotulados, reaproveitados por todas as cópias. Nesses, o "
        "acerto é verificado, não estimado.",
        f"- {lote['documentos'] - lote['documentos_avaliaveis']} documento(s) do "
        "checkpoint estão fora desta conta: ⚠️ (erro, formato não suportado, Nível 1 "
        "indisponível) e 📦 `.zip`. Acurácia sem cobertura ao lado é fácil de inflar "
        "jogando documento para ⚠️.",
        "",
        "## Como este número foi obtido",
        "",
        "1. Amostra estratificada de **documentos** (o que se quer estimar), com piso "
        "por estrato para dar voz aos estratos pequenos e de risco conhecido.",
        "2. Rótulo humano por **conteúdo** (sha256), cego ao veredito da tool — a "
        "imagem não diz o que ela decidiu, e positivos e negativos são rotulados na "
        "mesma pilha.",
        "3. Células de cada estrato multiplicadas por `N_estrato / rotulados_no_estrato` "
        "antes de somar, o que desfaz o oversampling.",
        "4. IC de Wilson 95% dimensionado pelo n bruto do denominador de cada taxa. "
        "Aproximação: o desenho estratificado tem variância um pouco diferente.",
        "",
        "Os rótulos ficam em "
        f"`{NOME_ROTULOS}`, chaveados por sha256 — reavalie qualquer execução futura "
        "contra eles (`validar.py avaliar`) para ver precisão e recall se moverem "
        "quando `--conf`, o modelo ou o fallback mudarem.",
    ]
    return "\n".join(linhas) + "\n"


def gerar(
    *,
    registros: list[dict[str, Any]],
    hashes: dict[str, str],
    pasta_saida: Path,
    execucao: dict[str, Any],
) -> dict[str, Path]:
    """Lê índice + rótulos, avalia, escreve `VALIDACAO.md`/`.json` e os rótulos."""
    indice = ler_indice(pasta_saida / NOME_INDICE)
    caminho_rotulos = pasta_saida / NOME_ROTULOS
    persistidos = carregar_rotulos(caminho_rotulos)

    # Rótulo persistido cobre a imagem que sobrou de uma amostra com outra
    # semente: o id sai do próprio sha256, sem depender do índice atual.
    conhecidos = {sha[:TAMANHO_ID]: sha for sha in persistidos}
    conhecidos.update({item.id: item.sha256 for item in indice})
    rotulos = {**persistidos, **ler_rotulos(pasta_saida / NOME_AMOSTRA, conhecidos=conhecidos)}
    salvar_rotulos(caminho_rotulos, rotulos)

    dados = avaliar(registros=registros, hashes=hashes, indice=indice, rotulos=rotulos)
    pasta_saida.mkdir(parents=True, exist_ok=True)
    caminhos = {
        "validacao_md": pasta_saida / "VALIDACAO.md",
        "validacao_json": pasta_saida / "VALIDACAO.json",
        "rotulos": caminho_rotulos,
    }
    caminhos["validacao_md"].write_text(
        montar_validacao(dados, execucao=execucao), encoding="utf-8"
    )
    caminhos["validacao_json"].write_text(
        json.dumps({"execucao": execucao, "validacao": dados}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return caminhos

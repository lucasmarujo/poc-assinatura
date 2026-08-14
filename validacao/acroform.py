"""Auditoria do sinal `pdf_embedded`: o campo `/Sig` está mesmo assinado?

`find_embedded_pdf_signatures` conta a **presença do widget** de assinatura no
AcroForm. Um formulário com um campo "assine aqui" nunca preenchido tem o widget
e não tem assinatura nenhuma — é a hipótese de falso positivo que este módulo
existe para medir.

O critério que decide, e que não pode ser confundido com nenhum outro:

| No PDF | Significa |
|---|---|
| campo `/FT /Sig` **sem** `/V` | placeholder — campo de assinatura em branco |
| `/V` com `/ByteRange` e `/Contents` não vazio | **assinado**: há PKCS#7 no arquivo |
| `/V` sem `/ByteRange` ou com `/Contents` vazio | assinatura preparada e não concluída |

**O nome do signatário não entra nisso.** `/V /Name` é opcional e vem `null` na
maioria dos PDFs do ICP-Brasil; `widget.field_value` do PyMuPDF devolve `""`
mesmo em assinatura criptográfica válida. Concluir "campo vazio" a partir do
nome vazio inverte o diagnóstico — o documento *está* assinado, o que falta é só
o rótulo de aparência. Quem quiser ver o nome, ele está dentro do certificado: a
varredura heurística de `nomes_no_certificado` puxa as cadeias em caixa alta do
PKCS#7, que é onde o CN costuma estar.

Só leitura, e sem dependência nova: `/ByteRange` sai do próprio PDF via PyMuPDF,
e o PKCS#7 é lido fatiando os bytes do arquivo no intervalo que o `/ByteRange`
deixa de fora — que é, por definição do formato, exatamente onde o `/Contents`
está.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from validacao.amostra import NOME_HASHES, carregar_hashes

NOME_RELATORIO = "ACROFORM.md"
NOME_CAMPOS = "ACROFORM.jsonl"

# PyMuPDF: PDF_WIDGET_TYPE_SIGNATURE. Mesmo valor que o `compat/` usa — é o
# widget que o sinal `pdf_embedded` conta.
TIPO_WIDGET_ASSINATURA = 6

# Abaixo disto o `/Contents` não comporta um PKCS#7: é campo reservado e não
# preenchido (assinatura interrompida no meio).
MINIMO_PKCS7_BYTES = 64

VEREDITO_ASSINADO = "assinado"
VEREDITO_CAMPO_VAZIO = "campo_vazio"
VEREDITO_INCOMPLETO = "incompleto"
VEREDITO_SEM_CAMPO = "sem_campo"
VEREDITO_ERRO = "erro"

DESCRICAO_VEREDITO: dict[str, str] = {
    VEREDITO_ASSINADO: "assinado — `/V` com `/ByteRange` e PKCS#7 no arquivo",
    VEREDITO_CAMPO_VAZIO: "campo vazio — widget `/Sig` sem `/V`: **falso positivo**",
    VEREDITO_INCOMPLETO: "incompleto — `/V` sem `/ByteRange` ou `/Contents` vazio",
    VEREDITO_SEM_CAMPO: "sem campo `/Sig` — o sinal não deveria ter sido contado",
    VEREDITO_ERRO: "não deu para ler o PDF",
}

GRUPO_SO_ACROFORM = "so_acroform"
GRUPO_COM_OUTRA_EVIDENCIA = "com_outra_evidencia"

DESCRICAO_GRUPO: dict[str, str] = {
    GRUPO_SO_ACROFORM: (
        "AcroForm é a **única** evidência — aqui o campo vazio derruba o veredito"
    ),
    GRUPO_COM_OUTRA_EVIDENCIA: (
        "AcroForm + carimbo digital e/ou Nível 1 — o veredito se sustenta sem ele"
    ),
}

# Cadeia em caixa alta com pelo menos duas palavras: é a forma do CN nos
# certificados ICP-Brasil ("FULANO DE TAL SILVA:12345678900"). Heurística
# assumida: o nome das ACs intermediárias vem em caixa mista e não casa.
_NOME_NO_CERTIFICADO = re.compile(rb"[A-Z][A-Z]+(?:[ ][A-Z][A-Z]*){1,6}")


@dataclass(frozen=True)
class CampoAssinatura:
    """Um campo `/Sig` do AcroForm, com o que decide se ele está assinado."""

    pagina: int
    nome: str | None
    tem_valor: bool
    tem_byterange: bool
    bytes_pkcs7: int
    subfilter: str | None = None
    nome_declarado: str | None = None
    data: str | None = None
    motivo: str | None = None
    nomes_no_certificado: list[str] = field(default_factory=list)

    @property
    def assinado(self) -> bool:
        return self.tem_valor and self.tem_byterange and self.bytes_pkcs7 >= MINIMO_PKCS7_BYTES


def veredito(campos: list[CampoAssinatura]) -> str:
    """O veredito do documento a partir dos campos lidos (puro)."""
    if not campos:
        return VEREDITO_SEM_CAMPO
    if any(campo.assinado for campo in campos):
        return VEREDITO_ASSINADO
    if any(campo.tem_valor for campo in campos):
        return VEREDITO_INCOMPLETO
    return VEREDITO_CAMPO_VAZIO


def _texto(valor: tuple[str, str] | None) -> str | None:
    """Valor de `xref_get_key` como texto, ou `None` quando a chave não existe."""
    if not valor or valor[0] == "null":
        return None
    return valor[1].strip("/").strip("()").strip() or None


def _byte_range_valido(valor: tuple[str, str] | None) -> bool:
    """`/ByteRange` é `[a b c d]`: o arquivo inteiro menos o buraco do
    `/Contents`. Quatro números crescentes, ou não é assinatura de verdade."""
    if not valor or valor[0] != "array":
        return False
    numeros = [int(n) for n in re.findall(r"-?\d+", valor[1])]
    return len(numeros) == 4 and numeros[0] >= 0 and numeros[0] + numeros[1] <= numeros[2]


def _desescapar(literal: str) -> bytes:
    """String literal de PDF (`(...)`) para bytes, resolvendo os escapes.

    O PKCS#7 é binário: o PyMuPDF o devolve com os bytes altos em octal
    (`\\332`), e sem desfazer isso a contagem de bytes sairia inflada e a
    varredura de nomes olharia o texto errado.
    """
    def substituir(encontrado: re.Match[str]) -> str:
        escapado = encontrado.group(1)
        if escapado[0] in "01234567":
            return chr(int(escapado, 8) & 0xFF)
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(escapado, escapado)

    return re.sub(r"\\([0-7]{1,3}|.)", substituir, literal).encode("latin-1", "replace")


def extrair_contents(objeto: str) -> bytes:
    """O PKCS#7 do dicionário de assinatura, hex (`<…>`) ou literal (`(…)`).

    Lido do objeto e não do arquivo pelo `/ByteRange`: o `/ByteRange` é um
    ponteiro, e ponteiro errado faria a auditoria medir bytes que não são a
    assinatura — exatamente o tipo de engano que ela existe para desfazer.
    """
    inicio = objeto.find("/Contents")
    if inicio < 0:
        return b""
    hexadecimal = re.match(r"/Contents\s*<([0-9A-Fa-f\s]*)>", objeto[inicio:])
    if hexadecimal:
        digitos = re.sub(r"\s", "", hexadecimal.group(1))
        return bytes.fromhex(digitos[: len(digitos) // 2 * 2])

    abre = objeto.find("(", inicio)
    if abre < 0:
        return b""
    profundidade, posicao = 1, abre + 1
    while posicao < len(objeto) and profundidade:
        caractere = objeto[posicao]
        if caractere == "\\":
            posicao += 1
        elif caractere == "(":
            profundidade += 1
        elif caractere == ")":
            profundidade -= 1
        posicao += 1
    return _desescapar(objeto[abre + 1 : posicao - 1]) if not profundidade else b""


def _nomes_no_certificado(dados: bytes) -> list[str]:
    """Cadeias em caixa alta dentro do PKCS#7 — o CN, quando dá para ler.

    Heurística de leitura, não parse de certificado: extrair o CN de verdade
    exigiria um decodificador ASN.1, e o veredito não depende do nome. Serve
    para o humano confirmar de relance que a assinatura é de gente, não um
    campo em branco.
    """
    achados: list[str] = []
    for encontrado in _NOME_NO_CERTIFICADO.findall(dados):
        nome = encontrado.decode("latin-1").strip()
        if nome not in achados:
            achados.append(nome)
    return achados[:5]


def _campo(doc: Any, widget: Any, pagina: int) -> CampoAssinatura:
    valor = doc.xref_get_key(widget.xref, "V")
    if not valor or valor[0] != "xref":
        return CampoAssinatura(
            pagina=pagina,
            nome=getattr(widget, "field_name", None),
            tem_valor=False,
            tem_byterange=False,
            bytes_pkcs7=0,
        )

    xref_valor = int(valor[1].split()[0])
    pkcs7 = extrair_contents(doc.xref_object(xref_valor, compressed=False))
    return CampoAssinatura(
        pagina=pagina,
        nome=getattr(widget, "field_name", None),
        tem_valor=True,
        tem_byterange=_byte_range_valido(doc.xref_get_key(xref_valor, "ByteRange")),
        bytes_pkcs7=len(pkcs7),
        subfilter=_texto(doc.xref_get_key(xref_valor, "SubFilter")),
        nome_declarado=_texto(doc.xref_get_key(xref_valor, "Name")),
        data=_texto(doc.xref_get_key(xref_valor, "M")),
        motivo=_texto(doc.xref_get_key(xref_valor, "Reason")),
        nomes_no_certificado=_nomes_no_certificado(pkcs7),
    )


def inspecionar(path: str | Path) -> list[CampoAssinatura]:
    """Os campos `/Sig` do PDF, com a evidência de cada um."""
    import pymupdf as _pymupdf

    pymupdf = cast(Any, _pymupdf)
    doc = pymupdf.open(str(path))
    try:
        return [
            _campo(doc, widget, indice + 1)
            for indice in range(doc.page_count)
            for widget in doc[indice].widgets() or []
            if getattr(widget, "field_type", None) == TIPO_WIDGET_ASSINATURA
        ]
    finally:
        doc.close()


# ---------- amostra -----------------------------------------------------------


def grupo(registro: dict[str, Any]) -> str | None:
    """Em qual grupo o documento entra, ou `None` se não tem sinal de AcroForm."""
    fontes = set(registro.get("fontes") or [])
    if "pdf_embedded" not in fontes:
        return None
    return GRUPO_SO_ACROFORM if fontes == {"pdf_embedded"} else GRUPO_COM_OUTRA_EVIDENCIA


def sortear(
    registros: list[dict[str, Any]],
    *,
    n_por_grupo: int,
    semente: int = 42,
    hashes: dict[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Amostra por grupo, um documento por conteúdo distinto.

    Embaralha e corta (como `validacao.amostra.sortear`): aumentar o `n` depois
    mantém a amostra anterior contida na nova. Sem a deduplicação por sha256, um
    contrato copiado 96 vezes tomaria a amostra inteira e ela mediria um PDF só.
    """
    por_grupo: dict[str, list[dict[str, Any]]] = {
        GRUPO_SO_ACROFORM: [],
        GRUPO_COM_OUTRA_EVIDENCIA: [],
    }
    for registro in registros:
        nome = grupo(registro)
        if nome:
            por_grupo[nome].append(registro)

    vistos: set[str] = set()
    amostra: dict[str, list[dict[str, Any]]] = {}
    for nome, candidatos in por_grupo.items():
        ordem = sorted(candidatos, key=lambda registro: str(registro["arquivo"]))
        random.Random(f"{semente}:{nome}").shuffle(ordem)
        escolhidos: list[dict[str, Any]] = []
        for registro in ordem:
            if len(escolhidos) >= n_por_grupo:
                break
            arquivo = str(registro["arquivo"])
            chave = (hashes or {}).get(arquivo, arquivo)
            if chave in vistos:
                continue
            vistos.add(chave)
            escolhidos.append(registro)
        amostra[nome] = escolhidos
    return amostra


def auditar(
    *,
    registros: list[dict[str, Any]],
    raiz_files: Path,
    n_por_grupo: int,
    semente: int = 42,
    hashes: dict[str, str] | None = None,
    ao_progredir: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Sorteia, abre cada PDF e devolve uma linha por documento auditado."""
    amostra = sortear(
        registros, n_por_grupo=n_por_grupo, semente=semente, hashes=hashes
    )
    total = sum(len(itens) for itens in amostra.values())
    linhas: list[dict[str, Any]] = []
    for nome, itens in amostra.items():
        for registro in itens:
            arquivo = str(registro["arquivo"])
            if ao_progredir is not None:
                ao_progredir(len(linhas) + 1, total, arquivo)
            try:
                campos = inspecionar(raiz_files / arquivo)
                resultado, erro = veredito(campos), None
            except Exception as exc:
                campos, resultado, erro = [], VEREDITO_ERRO, f"{type(exc).__name__}: {exc}"
            linhas.append(
                {
                    "arquivo": arquivo,
                    "grupo": nome,
                    "veredito": resultado,
                    "erro": erro,
                    "fontes": registro.get("fontes") or [],
                    "nivel0_total": registro.get("nivel0_total", 0),
                    "nivel1_total": registro.get("nivel1_total", 0),
                    "campos": [asdict(campo) | {"assinado": campo.assinado} for campo in campos],
                }
            )
    return linhas


# ---------- relatório ---------------------------------------------------------


def _populacao(registros: list[dict[str, Any]]) -> Counter[str]:
    return Counter(nome for registro in registros if (nome := grupo(registro)))


def montar_relatorio(
    linhas: list[dict[str, Any]], *, populacao: Counter[str], semente: int
) -> str:
    """`ACROFORM.md`: o veredito por grupo e o que ele significa para o lote."""
    por_grupo: dict[str, Counter[str]] = {}
    for linha in linhas:
        por_grupo.setdefault(linha["grupo"], Counter())[linha["veredito"]] += 1

    md = [
        "# AcroForm — o campo `/Sig` está mesmo assinado?",
        "",
        f"{len(linhas)} documento(s) do lote com sinal `pdf_embedded`, sorteados com "
        f"semente `{semente}` (um por conteúdo distinto) e reabertos para ler o "
        "`/V`, o `/ByteRange` e o PKCS#7 de cada campo `/Sig`.",
        "",
        "O que decide não é o nome do signatário: `/V /Name` é opcional e vem vazio na "
        "maioria dos PDFs do ICP-Brasil, e `widget.field_value` devolve `\"\"` mesmo em "
        "assinatura válida. Decide a presença do `/V` com `/ByteRange` e `/Contents` "
        "preenchido — só isso é assinatura criptográfica.",
        "",
        "## Veredito por grupo",
        "",
        "| Grupo | No lote | Auditados | " + " | ".join(DESCRICAO_VEREDITO) + " |",
        "|---" * (3 + len(DESCRICAO_VEREDITO)) + "|",
    ]
    for nome, contagem in por_grupo.items():
        md.append(
            f"| {DESCRICAO_GRUPO.get(nome, nome)} | {populacao.get(nome, 0)} "
            f"| {sum(contagem.values())} | "
            + " | ".join(str(contagem.get(chave, 0)) for chave in DESCRICAO_VEREDITO)
            + " |"
        )

    md += ["", "| Veredito | O que é |", "|---|---|"]
    md += [f"| `{chave}` | {texto} |" for chave, texto in DESCRICAO_VEREDITO.items()]

    criticos = por_grupo.get(GRUPO_SO_ACROFORM, Counter())
    auditados = sum(criticos.values())
    falsos = criticos.get(VEREDITO_CAMPO_VAZIO, 0) + criticos.get(VEREDITO_SEM_CAMPO, 0)
    md += ["", "## O que isso diz sobre a correção do `/V`", ""]
    if not auditados:
        md.append(
            "Nenhum documento auditado no grupo em que o AcroForm é a única evidência — "
            "é ele que decide se a correção muda algum veredito."
        )
    else:
        taxa = falsos / auditados
        md += [
            f"No grupo em que o AcroForm é a **única** evidência, {falsos} de "
            f"{auditados} documento(s) ({taxa:.1%}) têm o campo em branco ou nem campo "
            "`/Sig` — nesses, e só nesses, exigir o `/V` muda o veredito de ✅ para ❌.",
            "",
            f"Projetado para os {populacao.get(GRUPO_SO_ACROFORM, 0)} documentos desse "
            f"grupo no lote: **~{round(populacao.get(GRUPO_SO_ACROFORM, 0) * taxa):,} "
            "documento(s)** de falso positivo que a correção elimina."
            if populacao.get(GRUPO_SO_ACROFORM)
            else "",
        ]
        if not falsos:
            md += [
                "",
                "> Nenhum campo em branco na amostra. Exigir o `/V` continua sendo a "
                "leitura correta do formato — mas, neste lote, ela **não** é a fonte de "
                "falso positivo que se supunha: os campos estão assinados, o que está "
                "vazio é o nome. Antes de citar `find_embedded_pdf_signatures` como o "
                "maior contribuinte de erro, confira por qual critério a verificação "
                "anterior chamou o campo de vazio.",
            ]

    campos = [campo for linha in linhas for campo in linha["campos"]]
    assinados = [campo for campo in campos if campo["assinado"]]
    sem_nome = [campo for campo in assinados if not campo["nome_declarado"]]
    md += [
        "",
        "## O nome vazio não é campo vazio",
        "",
        f"Dos {len(campos)} campo(s) `/Sig` lidos, {len(assinados)} estão assinados — e "
        f"**{len(sem_nome)} deles ({len(sem_nome) / max(len(assinados), 1):.0%}) têm "
        "`/Name` vazio**. É o caso que engana: o PDF traz o PKCS#7 completo e não traz "
        "o rótulo de aparência, então quem olha `field_value`/`/Name` conclui \"campo em "
        "branco\" sobre um documento assinado.",
        "",
        f"Em {sum(1 for campo in assinados if campo['nomes_no_certificado'])} desses "
        "campos o nome do signatário foi recuperado de dentro do certificado — está no "
        "PKCS#7, não no dicionário do campo.",
        "",
        "> Limite desta auditoria: ela verifica que **existe** assinatura criptográfica "
        "(`/V` + `/ByteRange` + `/Contents`), não que ela é **válida** — cadeia ICP, "
        "integridade do documento e revogação exigiriam um verificador de certificado, "
        "que não está nas dependências do projeto.",
        "",
        "## Documentos auditados",
        "",
    ]
    for linha in linhas:
        campos = linha["campos"]
        assinados = sum(1 for campo in campos if campo["assinado"])
        nomes = sorted(
            {
                nome
                for campo in campos
                for nome in campo["nomes_no_certificado"]
            }
        )
        md += [
            f"### `{linha['arquivo']}`",
            "",
            f"- veredito: **{linha['veredito']}** · grupo: `{linha['grupo']}` · "
            f"fontes da tool: {', '.join(linha['fontes']) or '—'}",
            f"- campos `/Sig`: {len(campos)}, dos quais {assinados} assinado(s)",
        ]
        for campo in campos:
            md.append(
                f"  - `{campo['nome'] or '(sem nome)'}` — página {campo['pagina']} · "
                f"`/V` {'sim' if campo['tem_valor'] else 'não'} · "
                f"`/ByteRange` {'sim' if campo['tem_byterange'] else 'não'} · "
                f"PKCS#7 {campo['bytes_pkcs7']} bytes · "
                f"`/Name` {campo['nome_declarado'] or '(vazio)'} · "
                f"data {campo['data'] or '—'}"
            )
        if nomes:
            md.append(f"- nomes no certificado: {'; '.join(nomes)}")
        if linha["erro"]:
            md.append(f"- erro: {linha['erro']}")
        md.append("")

    return "\n".join(md) + "\n"


def gerar(
    *,
    registros: list[dict[str, Any]],
    raiz_files: Path,
    pasta_saida: Path,
    n_por_grupo: int,
    semente: int = 42,
    ao_progredir: Callable[[int, int, str], None] | None = None,
) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    """Audita a amostra e escreve `ACROFORM.md` + `ACROFORM.jsonl`."""
    linhas = auditar(
        registros=registros,
        raiz_files=raiz_files,
        n_por_grupo=n_por_grupo,
        semente=semente,
        hashes=carregar_hashes(pasta_saida / NOME_HASHES),
        ao_progredir=ao_progredir,
    )
    pasta_saida.mkdir(parents=True, exist_ok=True)
    caminhos = {
        "relatorio": pasta_saida / NOME_RELATORIO,
        "campos": pasta_saida / NOME_CAMPOS,
    }
    caminhos["relatorio"].write_text(
        montar_relatorio(linhas, populacao=_populacao(registros), semente=semente),
        encoding="utf-8",
    )
    with caminhos["campos"].open("w", encoding="utf-8") as arquivo:
        for linha in linhas:
            arquivo.write(json.dumps(linha, ensure_ascii=False) + "\n")
    return caminhos, linhas

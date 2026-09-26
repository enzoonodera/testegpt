# -*- coding: utf-8 -*-
"""
Fake mínimo da API síncrona do Playwright, usado SOMENTE em testes, para exercitar
o código real de _sistema/agendar_tiktok.py e _sistema/agendar_youtube.py sem
abrir um navegador de verdade.

Não é uma reimplementação da lógica dos agendadores — é um "double" da
interface (locator/click/get_attribute/etc.) que registra chamadas para provar,
de forma determinística, o que o código de produção realmente lê e verifica
(ou deixa de verificar) antes de prosseguir.

Suporta apenas o subconjunto de métodos realmente usados pelos dois agendadores.
Qualquer selector não registrado explicitamente pelo teste retorna um
FakeLocator vazio (count()==0), igual ao comportamento real do Playwright para
um seletor que não bate com nada.
"""
from __future__ import annotations

import re
import sys
import types
from unittest import mock


class FakeElement:
    def __init__(self, tag="div", text="", attrs=None, visible=True,
                 on_click=None, parent=None, call_log=None, children=None):
        self.tag = tag
        self.text = text
        self.attrs = dict(attrs or {})
        self.visible = visible
        self.on_click = on_click
        self.parent = parent
        self.call_log = call_log if call_log is not None else []
        # selector-exato -> FakeLocator (ou callable() -> FakeLocator), para
        # que produção possa fazer el.locator("algum.seletor.filho") e achar
        # uma estrutura real (ex.: item do timepicker -> span do texto),
        # igual ao registry de FakePage, mas escopado a ESTE elemento.
        self._children = dict(children or {})

    def _log(self, name):
        self.call_log.append(name)

    def is_visible(self):
        self._log("is_visible")
        return self.visible

    def is_enabled(self):
        self._log("is_enabled")
        return True

    def get_attribute(self, name):
        self._log(f"get_attribute:{name}")
        return self.attrs.get(name, "")

    def input_value(self):
        self._log("input_value")
        return self.attrs.get("value", "")

    def click(self, force=False):
        self._log("click")
        if self.on_click:
            self.on_click(self)

    def check(self, force=False):
        self._log("check")
        self.attrs["checked"] = "true"

    def is_checked(self):
        self._log("is_checked")
        return self.attrs.get("checked") == "true"

    def press(self, key):
        self._log(f"press:{key}")

    def fill(self, value):
        self._log("fill")
        self.attrs["value"] = value

    def scroll_into_view_if_needed(self):
        self._log("scroll_into_view_if_needed")

    def bounding_box(self):
        return {"width": 300, "height": 40}

    def inner_text(self, timeout=None):
        return self.text

    def evaluate(self, js, arg=None):
        self._log("evaluate")
        if "tagName" in js:
            return self.tag.upper()
        return None

    def locator(self, selector):
        # GATE 19.5 -- ESTÁGIO 2 (continuação, 2ª ocorrência do bug real
        # Windows): uma verificação exata em `_children` é tentada PRIMEIRO,
        # mesmo para seletores "xpath=...", para permitir que um teste
        # registre uma resposta ESPECÍFICA para um XPath distinto (ex.:
        # `xpath=ancestor::div[contains(@class,"card")][1]`, usado por
        # _find_checks_section() para subir vários níveis reais até o card,
        # diferente de um simples `xpath=..` de 1 nível usado em outro
        # lugar sobre o MESMO elemento). Sem isso, qualquer string que
        # começe com "xpath=" cairia sempre no mesmo fallback genérico
        # (devolver `self.parent`), impossibilitando simular dois XPaths
        # diferentes com destinos diferentes a partir do mesmo elemento.
        # Compatível com todo uso existente: nenhum teste registra a
        # string literal "xpath=.." (ou qualquer outra) em `_children`
        # hoje, então o fallback genérico abaixo continua sendo o caminho
        # realmente exercitado em todo o resto da suíte.
        entry = self._children.get(selector)
        if entry is not None:
            return entry() if callable(entry) else entry
        if selector.startswith("xpath="):
            return FakeLocator([self.parent] if self.parent else [])
        return FakeLocator([])


class FakeLocator:
    def __init__(self, elements):
        self._elements = list(elements)

    def count(self):
        return len(self._elements)

    def nth(self, i):
        return self._elements[i]

    @property
    def first(self):
        if not self._elements:
            raise IndexError("FakeLocator vazio: nenhum elemento casou com o seletor")
        return self._elements[0]

    def filter(self, has_text=None):
        if has_text is None:
            return FakeLocator(self._elements)
        pattern = has_text if hasattr(has_text, "match") else re.compile(re.escape(str(has_text)))
        return FakeLocator([e for e in self._elements if pattern.match(e.text)])

    def get_by_text(self, text, exact=True):
        return FakeLocator([e for e in self._elements if e.text == text])

    # Playwright locators that resolve to a single element also expose the
    # element-level API directly (auto-resolving to the sole match). Mirror
    # that here so production code written as `page.locator(x).get_attribute(y)`
    # works against this fake the same way it does against the real API.
    def get_attribute(self, name):
        return self.first.get_attribute(name) if self._elements else ""

    def is_visible(self):
        return self._elements[0].is_visible() if self._elements else False

    def click(self, force=False):
        self.first.click(force=force)

    def inner_text(self, timeout=None):
        return self.first.inner_text() if self._elements else ""


class FakeKeyboard:
    def __init__(self, call_log=None):
        self.call_log = call_log if call_log is not None else []

    def press(self, key):
        self.call_log.append(f"keyboard.press:{key}")

    def insert_text(self, text):
        self.call_log.append("keyboard.insert_text")


class FakePlaywrightContext:
    """Substitui o `BrowserContext` real do Playwright (o que
    `chromium.launch_persistent_context(...)` devolve), para testes de
    ponta a ponta de main()/process_prepared_batch() sem abrir um Chrome de
    verdade. `pages` começa vazio por padrão (como um perfil novo) --
    `.new_page()` cria uma FakePage e a registra, igual ao comportamento
    real."""

    def __init__(self, pages=None, page_factory=None):
        self.pages = list(pages) if pages else []
        self._page_factory = page_factory or (lambda: FakePage())
        self.closed = False
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        page = self._page_factory()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakePlaywrightChromium:
    def __init__(self, context_factory=None):
        self.launch_calls = []
        self._context_factory = context_factory or (lambda **kw: FakePlaywrightContext())

    def launch_persistent_context(self, **kwargs):
        self.launch_calls.append(kwargs)
        return self._context_factory(**kwargs)


class FakeSyncPlaywrightHandle:
    def __init__(self, context_factory=None):
        self.chromium = FakePlaywrightChromium(context_factory)


class FakeSyncPlaywrightCM:
    """Substitui `sync_playwright()` (a fábrica real do Playwright síncrono)
    para testes de ponta a ponta de main()/process_prepared_batch(), usado
    como `mock.patch("playwright.sync_api.sync_playwright", ...)` -- o alvo
    é o MÓDULO REAL do playwright, não o módulo do agendador, porque
    main()/process_prepared_batch() fazem `from playwright.sync_api import
    sync_playwright` localmente (dentro da função); o import local resolve
    o nome no momento da chamada, então patchar o atributo na origem
    funciona independentemente de onde o import local acontece.

    `context_factory(**launch_kwargs) -> FakePlaywrightContext` é
    injetável para simular, por exemplo, um contexto que já vem com uma
    página aberta."""

    def __init__(self, context_factory=None):
        self._context_factory = context_factory

    def __call__(self):
        # sync_playwright() é chamada como fábrica (sem argumentos) e o
        # resultado é usado como context manager (`with sync_playwright()
        # as p:`) -- devolver a si mesma deixa __call__ e __enter__/__exit__
        # no mesmo objeto, o suficiente para esse uso.
        return self

    def __enter__(self):
        return FakeSyncPlaywrightHandle(self._context_factory)

    def __exit__(self, exc_type, exc, tb):
        return False


def install_fake_playwright(sync_playwright_callable):
    """Substitui `playwright`/`playwright.sync_api` em `sys.modules` por
    módulos fake contendo apenas `sync_playwright = sync_playwright_callable`
    (tipicamente uma instância de FakeSyncPlaywrightCM), como um
    `mock.patch.dict` context manager.

    Necessário porque main()/process_prepared_batch() fazem
    `from playwright.sync_api import sync_playwright` localmente (dentro da
    função) -- não dá para simplesmente `mock.patch.object` o módulo do
    agendador, já que o nome só existe no escopo local depois do import.
    Como o Python resolve imports via `sys.modules` primeiro, substituir as
    entradas ali funciona tanto num ambiente ONDE o pacote real `playwright`
    está instalado (o caso do Windows real) quanto num ambiente onde ele
    não está (este sandbox de testes) -- o import local nunca chega a tocar
    o pacote real enquanto o patch estiver ativo."""
    fake_pkg = types.ModuleType("playwright")
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = sync_playwright_callable
    fake_pkg.sync_api = fake_sync_api
    return mock.patch.dict(sys.modules, {
        "playwright": fake_pkg,
        "playwright.sync_api": fake_sync_api,
    })


class FakePage:
    """
    registry: dict selector-exato -> FakeLocator (ou callable() -> FakeLocator,
    para simular estado que muda entre chamadas, como as listas do TikTok que
    são "recriadas" depois do primeiro clique).
    """
    def __init__(self, registry=None, url=""):
        self._registry = dict(registry or {})
        self.url = url
        self.keyboard = FakeKeyboard()

    def locator(self, selector):
        entry = self._registry.get(selector)
        if entry is None:
            return FakeLocator([])
        if callable(entry):
            return entry()
        return entry

    def get_by_text(self, text, exact=True, **kw):
        entry = self._registry.get(("get_by_text", text))
        if entry is None:
            return FakeLocator([])
        return entry() if callable(entry) else entry

    def evaluate(self, js, arg=None):
        return False

    def screenshot(self, path=None, full_page=True):
        pass

    def content(self):
        return "<html></html>"

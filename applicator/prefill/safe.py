"""
Submit protection for prefill. The guard cancels submit events and clicks on
submit-like buttons (page-level capture listeners plus network-level abort of
form-POST navigations) while filling, and is removed before the human takes
over. safe_click is the only sanctioned way to click, and refuses submit-like
targets.
"""

import re

# English plus common localized submit/send verbs (non-English ATS / LinkedIn UIs).
# Keep in sync with the RE in _GUARD_JS.
SUBMIT_RE = re.compile(
    r"submit|send application|\bsend\b|finish|complete application|confirm|"
    r"enviar|envoyer|soumettre|absenden|abschicken|einreichen|senden|inviare|invia\b|"
    r"verzenden|versturen|verstuur|indienen|skicka|gönder|odeslat|lähetä|wyślij|提交|送信|応募する|제출|отправить",
    re.I,
)

_GUARDED_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class SubmitRefused(RuntimeError):
    """Raised when code attempts to click something that looks like submit."""


def is_submit_like(text: str) -> bool:
    """
    desc: Whether a label/purpose string looks like a submit action.
    args:
        text (str): button text, aria-label, or purpose string.
    output:
        bool.
    """
    return bool(text and SUBMIT_RE.search(text))


_GUARD_JS = r"""
() => {
  if (window.__pfGuard) return true;
  const RE = /submit|send application|\bsend\b|finish|complete application|confirm|enviar|envoyer|soumettre|absenden|abschicken|einreichen|senden|inviare|invia\b|verzenden|versturen|verstuur|indienen|skicka|gönder|odeslat|lähetä|wyślij|提交|送信|応募する|제출|отправить/i;
  const g = { blocked: 0, orig: {}, handlers: [] };
  const label = el => [el.innerText, el.value, el.getAttribute('aria-label'), el.getAttribute('title')]
      .filter(Boolean).join(' ');
  const isSubmit = el => {
    const b = el && el.closest ? el.closest('button, input, a, [role=button]') : null;
    if (!b) return false;
    const tag = b.tagName.toLowerCase();
    // effective type: a type-less <button> inside a form is a submit button
    const t = ((tag === 'button' || tag === 'input') ? (b.type || '') : '').toLowerCase();
    if (t === 'submit' || t === 'image') return true;
    return RE.test(label(b));
  };
  const kill = e => { e.preventDefault(); e.stopImmediatePropagation(); g.blocked++; };
  // real target, even inside (open or closed) shadow DOM: e.target is retargeted to the host
  const origin = e => (e.composedPath && e.composedPath()[0]) || e.target;
  const onClick = e => { if (isSubmit(origin(e))) kill(e); };
  const onSubmit = e => kill(e);
  const onKey = e => {
    const t = origin(e);
    if (e.key === 'Enter' && t && t.tagName !== 'TEXTAREA' && t.form) kill(e);
  };
  [['click', onClick], ['auxclick', onClick], ['submit', onSubmit],
   ['keydown', onKey], ['keypress', onKey]].forEach(([n, h]) => {
    window.addEventListener(n, h, true); g.handlers.push([n, h]);
  });
  // Last line: cancel any form-originated navigation (GET or POST; sourceElement
  // is the form or its submitter, even inside closed shadow DOM), including
  // a form.submit() the page captured before the prototype override below.
  if (window.navigation && navigation.addEventListener) {
    const onNav = e => {
      const src = e.sourceElement;
      if (e.cancelable && (e.formData || (src && (src.tagName === 'FORM' || src.form)))) {
        e.preventDefault(); g.blocked++;
      }
    };
    navigation.addEventListener('navigate', onNav);
    g.navHandler = onNav;
  }
  const P = HTMLFormElement.prototype;
  g.orig.submit = P.submit; g.orig.requestSubmit = P.requestSubmit;
  P.submit = function () { g.blocked++; };
  P.requestSubmit = function () { g.blocked++; };
  window.__pfGuard = g;
  return true;
}
"""

_UNGUARD_JS = r"""
() => {
  const g = window.__pfGuard;
  if (!g) return 0;
  g.handlers.forEach(([n, h]) => window.removeEventListener(n, h, true));
  if (g.navHandler) navigation.removeEventListener('navigate', g.navHandler);
  HTMLFormElement.prototype.submit = g.orig.submit;
  HTMLFormElement.prototype.requestSubmit = g.orig.requestSubmit;
  delete window.__pfGuard;
  return g.blocked;
}
"""

_STATE: dict = {}  # id(page) -> {"route": fn, "popup_route": fn, "frame_cb": fn, "net_blocked": int}


def _inject(frame) -> None:
    try:
        frame.evaluate(_GUARD_JS)
    except Exception:
        pass  # detached / cross-process frame mid-navigation; re-injected on navigate


def route_should_block(request) -> bool:
    """
    desc: Network-level guard decision: a non-GET document navigation is a
        native form submission (or equivalent) and must be aborted while
        the guard is armed. XHR/fetch (autosave, resume parsing) still pass.
    args:
        request (playwright.sync_api.Request): intercepted request.
    output:
        bool: True if the request should be aborted.
    """
    return request.method.upper() in _GUARDED_METHODS and request.resource_type == "document"


def is_guarded(page) -> bool:
    """
    desc: Whether install_submit_guard is currently armed on a page.
    args:
        page (playwright.sync_api.Page): page to check.
    output:
        bool.
    """
    return id(page) in _STATE


def install_submit_guard(page) -> None:
    """
    desc: Arm submit protection on a page: capture-phase JS listeners in every
        frame cancel submit events, Enter-key implicit submission and clicks
        on submit-like buttons; form.submit()/requestSubmit() are neutralised;
        a route handler aborts non-GET document navigations (native form
        POSTs), and a context route aborts any document load in a popup this
        page opens (e.g. a target=_blank form). New/navigated frames are
        re-guarded. Call remove_submit_guard
        before handing the tab to the human.
    args:
        page (playwright.sync_api.Page): page to protect.
    output:
        None.
    """
    key = id(page)
    if key in _STATE:
        return
    state = {"net_blocked": 0}

    def _route(route):
        if route_should_block(route.request):
            state["net_blocked"] += 1
            route.abort()
        else:
            route.continue_()

    def _popup_route(route):
        # Popups are not covered by page.route. A new window's first document
        # request (e.g. a target=_blank form) arrives before its frame exists,
        # so any such request while this page is guarded is aborted.
        req = route.request
        try:
            pg = req.frame.page
            popup = pg is not page and pg.opener() is page
        except Exception:
            popup = req.is_navigation_request()  # frame not created yet: a new window
        if popup and req.resource_type == "document":
            state["net_blocked"] += 1
            route.abort()
        else:
            route.fallback()

    def _frame_cb(frame):
        _inject(frame)

    state["route"] = _route
    state["popup_route"] = _popup_route
    state["frame_cb"] = _frame_cb
    _STATE[key] = state
    page.route("**/*", _route)
    try:
        page.context.route("**/*", _popup_route)
    except Exception:
        state["popup_route"] = None
    page.on("framenavigated", _frame_cb)
    for fr in page.frames:
        _inject(fr)


def remove_submit_guard(page) -> int:
    """
    desc: Disarm the guard (listeners, overrides, route) so the human's own
        submit works normally.
    args:
        page (playwright.sync_api.Page): page previously guarded.
    output:
        int: number of submit attempts that were blocked while armed.
    """
    state = _STATE.pop(id(page), None)
    if state is None:
        return 0
    try:
        page.remove_listener("framenavigated", state["frame_cb"])
    except Exception:
        pass
    try:
        page.unroute("**/*", state["route"])
    except Exception:
        pass
    try:
        if state.get("popup_route"):
            page.context.unroute("**/*", state["popup_route"])
    except Exception:
        pass
    blocked = state["net_blocked"]
    for fr in page.frames:
        try:
            blocked += int(fr.evaluate(_UNGUARD_JS) or 0)
        except Exception:
            pass
    return blocked


_DESCRIBE_JS = r"""
el => ({
  tag: el.tagName.toLowerCase(),
  type: (el.getAttribute('type') || '').toLowerCase(),
  eff_type: (['button', 'input'].includes(el.tagName.toLowerCase()) ? (el.type || '') : '').toLowerCase(),
  in_form: !!el.form,
  text: (el.innerText || '').slice(0, 200),
  value: el.value || '',
  aria: el.getAttribute('aria-label') || '',
  title: el.getAttribute('title') || '',
})
"""


def _check_descriptor(d: dict, purpose: str) -> None:
    if is_submit_like(purpose):
        raise SubmitRefused(f"refusing click: purpose looks like submit ({purpose!r})")
    if d["tag"] in ("button", "input") and d["type"] in ("submit", "image"):
        raise SubmitRefused("refusing click: element is a type=submit control")
    if d.get("in_form") and d.get("eff_type") in ("submit", "image"):
        # <button> with no type inside a form defaults to submit
        raise SubmitRefused("refusing click: element is a form's default submit button")
    blob = " ".join([d["text"], str(d["value"]), d["aria"], d["title"]])
    if is_submit_like(blob):
        raise SubmitRefused(f"refusing click: element text looks like submit ({blob[:60]!r})")


def assert_not_submit(el, purpose: str = "") -> None:
    """
    desc: Raise SubmitRefused if an element looks like a submit control
        (type=submit/image, a type-less <button> inside a form, or
        text/value/aria-label/title matching submit|send application|send|
        finish|complete application|confirm).
    args:
        el (Locator | ElementHandle): element to check.
        purpose (str): optional description of why it would be clicked.
    output:
        None.
    """
    _check_descriptor(el.evaluate(_DESCRIBE_JS), purpose)


def safe_click(page_or_locator, purpose: str, selector: str | None = None, timeout: int = 5000) -> None:
    """
    desc: Click a NON-submitting control (open the form via 'Apply', open a
        dropdown, 'Add another', 'Next' between steps). Never use it to
        advance past a Review/Submit step. Refuses anything that
        looks like submit, and refuses a purpose string that looks like
        submit.
    args:
        page_or_locator (Page | Frame | Locator | ElementHandle): thing to
            click; if a Page/Frame, `selector` is required.
        purpose (str): short reason, e.g. 'open application form'.
        selector (str | None): CSS/text selector when given a Page/Frame.
        timeout (int): click timeout in ms.
    output:
        None. Raises SubmitRefused on submit-like targets.
    """
    target = page_or_locator
    if selector is not None:
        target = target.locator(selector).first
    elif hasattr(target, "frames") or hasattr(target, "child_frames"):
        raise ValueError("safe_click needs a selector when given a Page/Frame")
    elif hasattr(target, "first"):
        target = target.first
    if not purpose:
        raise ValueError("safe_click requires a purpose")
    assert_not_submit(target, purpose)
    target.click(timeout=timeout)

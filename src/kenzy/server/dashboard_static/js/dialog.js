// In-page confirm dialog — replaces window.confirm()/prompt(), which some
// mobile browsers block outright (a blocked confirm() returns false, so the
// Upgrade button silently did nothing on those phones). Promise-based and
// plain-DOM (no Preact coupling), so any view can `await confirmDialog(...)`
// where it previously called the native dialog.
//
//   confirmDialog("Delete this?")                          → Promise<boolean>
//   confirmDialog(msg, {confirmText: "Upgrade"})
//   confirmDialog(msg, {danger: true, typed: "REMOVE"})    — typed confirm:
//     the confirm button stays disabled until the user types the word.
//
// Esc / backdrop click / Cancel resolve false; Enter confirms when allowed.
// Message text is set via textContent — person names etc. can never inject.

export function confirmDialog(message, opts = {}) {
  const { title = "", confirmText = "Confirm", cancelText = "Cancel", danger = false, typed = null } = opts;
  return new Promise((resolve) => {
    const prevFocus = document.activeElement;
    const wrap = document.createElement("div");
    wrap.className = "dlg-wrap";
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-modal", "true");

    const box = document.createElement("div");
    box.className = "dlg";
    wrap.appendChild(box);

    if (title) {
      const h = document.createElement("h3");
      h.textContent = title;
      box.appendChild(h);
    }
    const p = document.createElement("p");
    p.className = "dlg-msg";
    p.textContent = message;
    box.appendChild(p);

    let input = null;
    if (typed) {
      const lab = document.createElement("p");
      lab.className = "dlg-typed micro";
      lab.append("Type ");
      const code = document.createElement("span");
      code.className = "mono";
      code.textContent = typed;
      lab.appendChild(code);
      lab.append(" to confirm:");
      box.appendChild(lab);
      input = document.createElement("input");
      input.type = "text";
      input.className = "dlg-input mono";
      input.autocapitalize = "off";
      input.autocomplete = "off";
      input.spellcheck = false;
      box.appendChild(input);
    }

    const row = document.createElement("div");
    row.className = "dlg-row";
    const cancel = document.createElement("button");
    cancel.className = "btn-ghost";
    cancel.textContent = cancelText;
    const ok = document.createElement("button");
    ok.className = danger ? "btn-danger" : "btn-primary";
    ok.textContent = confirmText;
    if (typed) ok.disabled = true;
    row.appendChild(cancel);
    row.appendChild(ok);
    box.appendChild(row);

    function close(result) {
      document.removeEventListener("keydown", onKey, true);
      wrap.remove();
      if (prevFocus && prevFocus.focus) prevFocus.focus();
      resolve(result);
    }
    function confirmOk() {
      if (typed && (!input || input.value.trim() !== typed)) return;
      close(true);
    }
    function onKey(e) {
      if (e.key === "Escape") {
        e.stopPropagation();
        close(false);
      } else if (e.key === "Enter" && !(e.target && e.target.tagName === "BUTTON")) {
        e.preventDefault();
        confirmOk();
      }
    }

    cancel.addEventListener("click", () => close(false));
    ok.addEventListener("click", confirmOk);
    wrap.addEventListener("click", (e) => {
      if (e.target === wrap) close(false);
    });
    if (input) input.addEventListener("input", () => (ok.disabled = input.value.trim() !== typed));
    document.addEventListener("keydown", onKey, true);

    document.body.appendChild(wrap);
    (input || (danger ? cancel : ok)).focus();
  });
}

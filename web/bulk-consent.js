(() => {
  const selected = new Set();
  const key = () => sessionStorage.getItem("disparos-app-key");

  function updateControls() {
    const count = selected.size;
    document.querySelectorAll("[data-bulk-consent-action]").forEach((button) => {
      button.disabled = count === 0;
      button.textContent = `${button.dataset.bulkConsentAction === "grant" ? "Autorizar" : "Remover autorização"} (${count})`;
    });
    const selectAll = document.querySelector("[data-bulk-consent-select-all]");
    if (selectAll) {
      const boxes = [...document.querySelectorAll("[data-bulk-contact-id]")];
      selectAll.checked = boxes.length > 0 && boxes.every((box) => box.checked);
      selectAll.indeterminate = boxes.some((box) => box.checked) && !selectAll.checked;
    }
  }

  function installControls() {
    const body = document.getElementById("contacts-table-body");
    if (!body) return;
    const table = body.closest("table");
    if (!table) return;
    const wrap = table.closest(".table-wrap");

    if (wrap && !wrap.previousElementSibling?.hasAttribute("data-bulk-consent-toolbar")) {
      const toolbar = document.createElement("div");
      toolbar.dataset.bulkConsentToolbar = "";
      toolbar.className = "actions";
      toolbar.style.margin = "0 0 12px";
      toolbar.innerHTML = `
        <button class="btn primary" type="button" data-bulk-consent-action="grant" disabled>Autorizar (0)</button>
        <button class="btn" type="button" data-bulk-consent-action="revoke" disabled>Remover autorização (0)</button>
        <span class="help">A autorização deve corresponder ao consentimento real de cada contato.</span>`;
      wrap.parentNode.insertBefore(toolbar, wrap);
    }

    const header = table.querySelector("thead tr");
    if (header && !header.querySelector("[data-bulk-consent-select-all]")) {
      const th = document.createElement("th");
      th.innerHTML = '<input type="checkbox" aria-label="Selecionar contatos visíveis" data-bulk-consent-select-all style="width:auto">';
      header.insertBefore(th, header.firstElementChild);
    }

    body.querySelectorAll("tr").forEach((row) => {
      const consentButton = row.querySelector("[data-consent]");
      if (!consentButton) return;
      const id = consentButton.dataset.consent;
      row.dataset.bulkContactId = id;
      let cell = row.querySelector("[data-bulk-consent-cell]");
      if (!cell) {
        cell = document.createElement("td");
        cell.dataset.bulkConsentCell = "";
        cell.innerHTML = `<input type="checkbox" data-bulk-contact-id="${id}" aria-label="Selecionar contato" style="width:auto">`;
        row.insertBefore(cell, row.firstElementChild);
      }
      const checkbox = cell.querySelector("input");
      checkbox.checked = selected.has(id);
    });
    updateControls();
  }

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-bulk-contact-id]")) {
      event.target.checked ? selected.add(event.target.dataset.bulkContactId) : selected.delete(event.target.dataset.bulkContactId);
      updateControls();
    } else if (event.target.matches("[data-bulk-consent-select-all]")) {
      document.querySelectorAll("[data-bulk-contact-id]").forEach((checkbox) => {
        checkbox.checked = event.target.checked;
        event.target.checked ? selected.add(checkbox.dataset.bulkContactId) : selected.delete(checkbox.dataset.bulkContactId);
      });
      updateControls();
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-bulk-consent-action]");
    if (!button || !selected.size) return;
    const optedIn = button.dataset.bulkConsentAction === "grant";
    const count = selected.size;
    const prompt = optedIn
      ? `Confirmar autorização de campanhas para ${count} contato(s)? Faça isso somente para pessoas que realmente consentiram em receber mensagens.`
      : `Remover a autorização de campanhas de ${count} contato(s)?`;
    if (!window.confirm(prompt)) return;

    button.disabled = true;
    try {
      const headers = { "Content-Type": "application/json" };
      if (key()) headers["X-App-Key"] = key();
      const response = await fetch("/api/contacts/consent-bulk", {
        method: "POST",
        headers,
        body: JSON.stringify({ contact_ids: [...selected], opted_in: optedIn })
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || `Falha HTTP ${response.status}`);
      sessionStorage.setItem("bulk-consent-return-to-contacts", "1");
      window.location.reload();
    } catch (error) {
      window.alert(error.message);
      updateControls();
    }
  });

  const observer = new MutationObserver(installControls);
  observer.observe(document.getElementById("content") || document.body, { childList: true, subtree: true });
  installControls();

  if (sessionStorage.getItem("bulk-consent-return-to-contacts")) {
    sessionStorage.removeItem("bulk-consent-return-to-contacts");
    setTimeout(() => document.querySelector('.nav button[data-page="contacts"]')?.click(), 0);
  }
})();

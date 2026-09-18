(function () {
  const body = document.body;
  const base = (body && body.dataset.ingressPath) || "";
  if (!base) return;

  document.querySelectorAll("[data-nav-link]").forEach(function (link) {
    const href = link.getAttribute("href") || "";
    if (!href.startsWith("/") || href.startsWith(base + "/")) return;
    link.setAttribute("href", base + href);
  });
})();

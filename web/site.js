(() => {
  "use strict";
  const KEY = "fpe-theme";
  const btn = document.getElementById("themeBtn");
  const paint = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    if (btn) btn.innerHTML =
      '<svg class="ico" aria-hidden="true"><use href="icons.svg#i-' + (dark ? "sun" : "moon") +
      '"></use></svg> ' + (dark ? "Light" : "Dark");
  };
  paint();
  if (btn) btn.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(KEY, next); } catch (e) { /* private mode: theme resets on reload */ }
    paint();
  });
})();

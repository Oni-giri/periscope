(function () {
  var root = document.documentElement;
  var storedTheme = localStorage.getItem("periscope-theme");
  var storedAccent = localStorage.getItem("periscope-accent");
  var prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = storedTheme || (prefersDark ? "dark" : "light");
  root.dataset.accent = storedAccent || "red";
})();

(function () {
  var root = document.documentElement;

  function updateLabels() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.textContent = root.dataset.theme === "dark" ? "Light" : "Dark";
    });
    document.querySelectorAll("[data-accent-toggle]").forEach(function (button) {
      button.textContent = root.dataset.accent === "blue" ? "Red" : "Blue";
    });
  }

  document.addEventListener("click", function (event) {
    var themeButton = event.target.closest("[data-theme-toggle]");
    if (themeButton) {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      localStorage.setItem("periscope-theme", root.dataset.theme);
      updateLabels();
    }

    var accentButton = event.target.closest("[data-accent-toggle]");
    if (accentButton) {
      root.dataset.accent = root.dataset.accent === "blue" ? "red" : "blue";
      localStorage.setItem("periscope-accent", root.dataset.accent);
      updateLabels();
    }
  });

  document.body.addEventListener("htmx:responseError", function () {
    var error = document.querySelector("[data-request-error]");
    if (!error) return;
    error.textContent = "The request failed. The stored page has not changed.";
    error.hidden = false;
  });

  updateLabels();
})();

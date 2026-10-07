var savedTheme = null;
try { savedTheme = localStorage.getItem("theme"); } catch (e) {}
if (savedTheme === null && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
  savedTheme = "dark";
}
if (savedTheme === "dark") {
  document.documentElement.setAttribute("data-theme", "dark");
} else {
  document.documentElement.setAttribute("data-theme", "light");
}

document.addEventListener("DOMContentLoaded", function () {

  var themeButtons = document.querySelectorAll(".theme-button");
  for (var i = 0; i < themeButtons.length; i++) {
    themeButtons[i].addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      var newTheme = (current === "dark") ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", newTheme);
      try { localStorage.setItem("theme", newTheme); } catch (e) {}
    });
  }

  var showButtons = document.querySelectorAll(".show-button");
  for (var j = 0; j < showButtons.length; j++) {
    showButtons[j].addEventListener("click", function () {
      var box = this.parentElement.querySelector("input");
      if (box.type === "password") {
        box.type = "text";
        this.textContent = "Hide";
      } else {
        box.type = "password";
        this.textContent = "Show";
      }
    });
  }

  var confirmForms = document.querySelectorAll("form[data-confirm]");
  for (var c = 0; c < confirmForms.length; c++) {
    confirmForms[c].addEventListener("submit", function (event) {
      if (!window.confirm(this.getAttribute("data-confirm"))) { event.preventDefault(); }
    });
  }

  var busyButtons = document.querySelectorAll("button[data-busy]");
  for (var b = 0; b < busyButtons.length; b++) {
    busyButtons[b].form.addEventListener("submit", function () {
      var btn = this.querySelector("button[data-busy]");
      btn.textContent = btn.getAttribute("data-busy");
      setTimeout(function () { btn.disabled = true; }, 0);
    });
  }

  var registerForm = document.getElementById("register-form");
  if (registerForm) {
    registerForm.addEventListener("submit", function (event) {
      var password = document.getElementById("password").value;
      var confirm = document.getElementById("confirm_password").value;
      if (password.length < 8) {
        alert("Password must be at least 8 characters.");
        event.preventDefault();
      } else if (password !== confirm) {
        alert("Passwords do not match.");
        event.preventDefault();
      }
    });
  }

  setTimeout(function () {
    var messages = document.querySelectorAll(".message");
    for (var k = 0; k < messages.length; k++) {
      if (messages[k].classList.contains("success")) { messages[k].style.display = "none"; }
    }
  }, 5000);
});

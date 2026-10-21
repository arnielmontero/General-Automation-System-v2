// Record page: × on a child SO chip asks for the reason, then removes the SO and its data.
(function () {
  const el = document.getElementById("so-remove-modal");
  if (!el) return;
  const form = document.getElementById("so-remove-form");
  const comment = document.getElementById("so-remove-comment");
  el.addEventListener("show.bs.modal", function (e) {
    const b = e.relatedTarget;
    form.action = b.dataset.url;
    document.getElementById("so-remove-name").textContent = b.dataset.so;
    const parts = [b.dataset.docs + " packing list(s)", "its MO's and transfers"];
    if (b.dataset.children !== "0") parts.unshift(b.dataset.children + " child SO(s) under it");
    document.getElementById("so-remove-what").textContent = "This also removes " + parts.join(", ") + ".";
    comment.value = "";
  });
  el.addEventListener("shown.bs.modal", function () { comment.focus(); });
  form.addEventListener("submit", function (e) {
    if (!comment.value.trim()) {
      e.preventDefault();
      comment.classList.add("is-invalid");
      comment.focus();
      return;
    }
    form.querySelector(".btn-danger").disabled = true;
  });
})();

// Keyboard: Enter / Space on a focused chip opens the popup like a click.
document.addEventListener("keydown", function (e) {
  const chip = e.target.closest && e.target.closest(".so-chip.is-removable");
  if (chip && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    chip.click();
  }
});

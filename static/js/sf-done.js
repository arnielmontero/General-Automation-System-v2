// SF-Innova / SF-Electrical checkmarks: ask Yes / No first; unchecking (Admin) needs a reason.
(function () {
  const el = document.getElementById("sf-modal");
  if (!el) return;
  const modal = bootstrap.Modal.getOrCreateInstance(el);
  const title = document.getElementById("sf-modal-title");
  const message = document.getElementById("sf-modal-message");
  const commentWrap = document.getElementById("sf-modal-comment-wrap");
  const comment = document.getElementById("sf-modal-comment");
  const yes = document.getElementById("sf-modal-yes");
  let box = null;

  document.addEventListener("click", function (e) {
    const target = e.target.closest(".sf-check");
    if (!target) return;
    e.preventDefault(); // the box changes only after "Yes" and the server's answer
    if (target.disabled) return;
    box = target;
    const undo = box.dataset.sfUndo === "1";
    title.textContent = box.dataset.sfTitle;
    message.textContent = box.dataset.sfMessage;
    commentWrap.classList.toggle("d-none", !undo);
    comment.value = "";
    comment.classList.remove("is-invalid");
    yes.className = "btn btn-sm " + (undo ? "btn-danger" : "btn-primary");
    modal.show();
  });

  el.addEventListener("shown.bs.modal", function () {
    (box && box.dataset.sfUndo === "1" ? comment : yes).focus();
  });

  yes.addEventListener("click", function () {
    if (!box) return;
    const undo = box.dataset.sfUndo === "1";
    if (undo && !comment.value.trim()) {
      comment.classList.add("is-invalid");
      comment.focus();
      return;
    }
    yes.disabled = true;
    htmx
      .ajax("POST", box.dataset.sfUrl, {
        source: box,
        target: box.dataset.sfTarget,
        swap: "outerHTML",
        values: undo ? { comment: comment.value.trim() } : {},
      })
      .finally(function () {
        yes.disabled = false;
      });
    modal.hide();
  });
})();

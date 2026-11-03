/* Alambic UI — menus du bandeau : un seul ouvert à la fois, fermeture au clic extérieur et sur Échap.
   Facultatif : sans ce script, les menus (<details>) restent utilisables. */
(function () {
  function menus() { return document.querySelectorAll('details.al-menu'); }
  document.addEventListener('click', function (e) {
    menus().forEach(function (d) { if (d.open && !d.contains(e.target)) d.open = false; });
  });
  document.addEventListener('toggle', function (e) {
    var d = e.target;
    if (!(d instanceof HTMLDetailsElement) || !d.classList.contains('al-menu') || !d.open) return;
    menus().forEach(function (o) { if (o !== d) o.open = false; });
  }, true);
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    menus().forEach(function (d) {
      if (d.open) { d.open = false; var s = d.querySelector('summary'); if (s) s.focus(); }
    });
  });
})();

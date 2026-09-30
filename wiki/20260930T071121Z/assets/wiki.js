'use strict';
document.body.classList.remove('no-js');
const search = document.getElementById('page-search');
if (search) {
  const cards = [...document.querySelectorAll('.card')];
  const status = document.getElementById('search-status');
  search.addEventListener('input', () => {
    const terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    let count = 0;
    for (const card of cards) {
      const match = terms.every(term => card.dataset.search.toLowerCase().includes(term));
      card.hidden = !match;
      if (match) count += 1;
    }
    status.textContent = `${count} of ${cards.length} topics shown`;
  });
}

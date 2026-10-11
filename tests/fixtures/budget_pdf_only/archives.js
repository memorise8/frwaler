/* archives.js */

const ui = {
  en: {
    decimal: '',
    emptyTable: 'No data available',
    info: 'Showing _START_ to _END_ of _TOTAL_ entries',
    infoEmpty: 'Showing 0 to 0 of 0 entries',
    infoFiltered: '(filtered from _MAX_ total entries)',
    infoPostFix: '',
    thousands: ',',
    lengthMenu: 'Show _MENU_ entries',
    loadingRecords: 'Loading…',
    processing: 'Processing…',
    search: 'Search:',
    zeroRecords: 'No matching records found',
    paginate: { first: 'First', last: 'Last', next: 'Next', previous: 'Previous' },
    aria: {
      sortAscending: ': activate to sort column ascending',
      sortDescending: ': activate to sort column descending'
    }
  },
  fr: {
    decimal: ',',
    emptyTable: 'Aucune donnée à afficher',
    info: "Affichage de l'élément _START_ à _END_ sur _TOTAL_ éléments",
    infoEmpty: "Affichage de l'élément 0 à 0 sur 0 éléments",
    infoFiltered: '(filtré de _MAX_ éléments au total)',
    infoPostFix: '',
    thousands: ' ',
    lengthMenu: 'Afficher _MENU_ éléments',
    loadingRecords: 'Chargement…',
    processing: 'Traitement…',
    search: 'Rechercher&nbsp;:',
    zeroRecords: 'Aucun élément correspondant',
    paginate: { first: 'Premier', previous: 'Précédent', next: 'Suivant', last: 'Dernier' },
    aria: {
      sortAscending: ': activer pour trier la colonne par ordre croissant',
      sortDescending: ': activer pour trier la colonne décroissant'
    }
  }
};

const lang = document.documentElement.getAttribute('lang') === 'fr' ? 'fr' : 'en';

const headings = {
  en: ['Date', 'Title', 'Prime Minister', 'Finance Minister', 'Documents'],
  fr: ['Date', 'Titre', 'Premier ministre', 'Ministre des Finances', 'Documents']
};

const newTab = {
  en: 'target="_blank" rel="noopener noreferrer" aria-label="This link opens in a new tab"',
  fr: 'target="_blank" rel="noopener noreferrer" aria-label="Ce lien s’ouvre dans un nouvel onglet"'
};

const pageTitle = document.title || '';
const isBudgetPage = /budget/i.test(pageTitle);
const jsonFile = isBudgetPage ? 'budget.json' : 'fiscal.json';

function safeText(value, fallback = '') {
  if (typeof value !== 'string') return fallback;
  const trimmed = value.trim();
  return trimmed.length ? trimmed : fallback;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function getLocalizedValue(value, fallback = '') {
  if (value && typeof value === 'object') {
    return safeText(value[lang], fallback);
  }

  if (typeof value === 'string') {
    return safeText(value, fallback);
  }

  return fallback;
}

function setResponsiveLabels() {
  $('#budget-table tbody tr').each(function () {
    $(this).find('td').each(function (idx) {
      $(this).attr({
        'data-label': `${headings[lang][idx]}${lang === 'fr' ? ' ' : ''}: `,
        title: headings[lang][idx]
      });
    });
  });
}

function initVisibleLightboxes() {
  $('#budget-table .wb-lbx').not('.wb-inited').each(function () {
    $(this).trigger('wb-init.wb-lbx');
  });
}

$(document).on('wb-ready.wb', function () {
  const $table = $('#budget-table');

  let $modalHolder = $('#budget-lightbox-modals');

  if (!$modalHolder.length) {
    $modalHolder = $('<div id="budget-lightbox-modals"></div>');
    $table.closest('.table-responsive').after($modalHolder);
  }


  const table = $table.DataTable({
    columns: headings[lang].map(function (heading) {
      return {
        title: heading,
        name: heading
      };
    }),
    columnDefs: [
      { width: '10%', targets: 0 },
      { width: '25%', targets: 1 },
      { width: '20%', targets: 2 },
      { width: '20%', targets: 3 },
      { width: '25%', targets: 4 }
    ],
    language: ui[lang],
    paging: true,
    lengthMenu: [10, 25, 50, 100],
    order: [[0, 'desc']],
    autoWidth: false,
    deferRender: true,
    processing: true,
    dom: '<"top"f>rt<"bottom"lp><"clear">',
    drawCallback: function () {
      setResponsiveLabels();
      initVisibleLightboxes();
    }
  });

  $.getJSON(jsonFile)
    .done(function (data) {
      const rows = [];
      const modals = [];
      const items = Array.isArray(data && data.data) ? data.data : [];

      items.forEach(function (item, index) {
        item = item || {};

        const dateStr = safeText(item.date, '');
        const titleText = getLocalizedValue(
          item.title,
          lang === 'fr' ? 'Titre non disponible' : 'Title not available'
        );

        const pMin = safeText(item.pMin, '');
        const financeMinister = getLocalizedValue(item.fMin, '');

        let titleCell = escapeHtml(titleText);

        if (item.website && typeof item.website === 'object' && item.website[lang]) {
          const href = escapeHtml(safeText(item.website[lang], '#'));
          titleCell = `<a href="${href}" ${newTab[lang]}>${escapeHtml(titleText)}</a>`;
        }

        const publications = Array.isArray(item.publications && item.publications[lang])
          ? item.publications[lang]
          : [];

        let docsCell = lang === 'fr' ? 'Aucun PDF' : 'No PDFs';

        if (publications.length) {
          const modalId = `budget-lightbox-${index}`;
          const docLabel = lang === 'fr' ? 'PDF disponibles' : 'Available PDFs';
          const docAria = lang === 'fr'
            ? `PDF disponibles pour ${titleText}`
            : `Available PDFs for ${titleText}`;

          const pubsList = publications.map(function (publication) {
            const pTitle = safeText(publication && publication.title, lang === 'fr' ? 'Document' : 'Document');
            const pPath = safeText(publication && publication.path, '#');

            return `<li><a href="${escapeHtml(pPath)}" ${newTab[lang]}>${escapeHtml(pTitle)}</a></li>`;
          }).join('');

          docsCell = `<a href="#${modalId}" class="wb-lbx" aria-controls="${modalId}" aria-label="${escapeHtml(docAria)}">${escapeHtml(docLabel)}</a>`;

          modals.push(`
            <section id="${modalId}" class="mfp-hide modal-dialog modal-content overlay-def">
              <header class="modal-header">
                <div class="modal-title">
                  <h2 class="mrgn-tp-0 mrgn-bttm-0">${escapeHtml(titleText)}</h2>
                  <small>${escapeHtml(dateStr)}</small>
                </div>
              </header>
              <div class="modal-body">
                <ul>
                  ${pubsList}
                </ul>
              </div>
            </section>
          `);
        }

        rows.push([
          escapeHtml(dateStr),
          titleCell,
          escapeHtml(pMin),
          escapeHtml(financeMinister),
          docsCell
        ]);
      });

      $modalHolder.html(modals.join(''));

      table.clear();

      if (rows.length) {
        table.rows.add(rows).draw(false);
      } else {
        table.draw(false);
      }

      setResponsiveLabels();
      initVisibleLightboxes();
    })
    .fail(function (jqXHR, textStatus, errorThrown) {
      console.error('JSON fetch failed:', textStatus, errorThrown);

      const msg = lang === 'fr'
        ? 'Erreur de chargement des données.'
        : 'Failed to load data.';

      table.clear().draw(false);

      $('#budget-table tbody').html(
        `<tr><td colspan="${headings[lang].length}" role="alert" class="text-danger">${escapeHtml(msg)}</td></tr>`
      );
    });
});
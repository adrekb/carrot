// ===== Select menus that belong to this app =====
//
// A native `<select>` closed looks like everything else here: the CSS on
// `select` gives it the app's border, radius, font and colours. Opened, none
// of that applies — the popup is drawn by the OS, not the page. On Windows
// that meant a white list in the system font over a dark app, sized to the
// longest option rather than to anything on screen, and long lists (the model
// picker, which is every model on every configured provider) ran past the
// bottom of the window with the selected row somewhere off in the middle.
// There is no CSS that fixes this; the popup is not in the document.
//
// So the popup is replaced and the `<select>` is kept. Every call site keeps
// reading `.value`, every `onchange=` in the markup keeps firing, and a
// `<select>` built by JS three tabs from here is upgraded the moment it is
// clicked without knowing this file exists. The alternative — a component
// each call site has to adopt — would have converted maybe six of the
// twenty-eight menus in the app and left the rest looking like Windows.

(function () {
    'use strict';

    // The menu that is open, if any. Only ever one: opening a second closes
    // the first, the same way a native popup behaves.
    let open = null;

    function isUpgradable(el) {
        if (!el || el.tagName !== 'SELECT') return false;
        // A multiple/sized select is a list box drawn inline, not a popup —
        // it has no OS popup to replace and intercepting its clicks would
        // break the one interaction it has.
        if (el.multiple || el.size > 1) return false;
        if (el.disabled || el.dataset.nativeMenu !== undefined) return false;
        return true;
    }

    // --- building the menu ---

    function buildMenu(select) {
        const menu = document.createElement('div');
        menu.className = 'dd-menu';
        menu.setAttribute('role', 'listbox');
        const rows = [];

        for (const option of Array.from(select.options)) {
            // An <optgroup> label is a heading, not something to land on.
            // Emitted when the group changes rather than by walking children,
            // so options outside any group still come out in document order.
            const group = option.parentElement;
            if (group && group.tagName === 'OPTGROUP' && group !== menu._group) {
                menu._group = group;
                const head = document.createElement('div');
                head.className = 'dd-group';
                head.textContent = group.label;
                menu.appendChild(head);
            }
            const row = document.createElement('div');
            row.className = 'dd-item' + (option.disabled ? ' disabled' : '')
                          + (option.selected ? ' on' : '');
            row.setAttribute('role', 'option');
            row.setAttribute('aria-selected', option.selected ? 'true' : 'false');
            const mark = document.createElement('span');
            mark.className = 'dd-mark';
            mark.textContent = option.selected ? '✓' : '';
            const text = document.createElement('span');
            text.className = 'dd-text';
            // textContent, not innerHTML: option labels include model ids
            // that come back from a provider's API, and those are somebody
            // else's strings.
            text.textContent = option.textContent;
            row.appendChild(mark);
            row.appendChild(text);
            row._option = option;
            if (!option.disabled) rows.push(row);
            menu.appendChild(row);
        }

        menu._rows = rows;
        return menu;
    }

    // --- placement ---
    //
    // Fixed, because the trigger can be inside a scrolling panel and an
    // absolutely positioned menu would be clipped by it. Fixed means page
    // scroll moves the trigger out from under the menu, which is why scroll
    // closes it below.

    function place(menu, select) {
        const rect = select.getBoundingClientRect();
        const margin = 8;
        // At least as wide as the control it came from — a menu narrower than
        // its trigger reads as a different control — and free to grow to its
        // content up to what the viewport can hold.
        menu.style.minWidth = rect.width + 'px';
        menu.style.maxWidth = Math.max(rect.width, window.innerWidth - margin * 2) + 'px';

        const below = window.innerHeight - rect.bottom - margin;
        const above = rect.top - margin;
        // Prefer below unless the list would be squeezed into less room than
        // it has above. The cap is what stopped the model list running off
        // the bottom of the window.
        const up = below < Math.min(menu.scrollHeight, 240) && above > below;
        menu.style.maxHeight = Math.max(120, Math.floor(up ? above : below)) + 'px';

        const width = menu.getBoundingClientRect().width;
        let left = rect.left;
        if (left + width > window.innerWidth - margin) left = window.innerWidth - margin - width;
        menu.style.left = Math.max(margin, left) + 'px';
        if (up) {
            menu.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
            menu.style.top = 'auto';
        } else {
            menu.style.top = (rect.bottom + 4) + 'px';
            menu.style.bottom = 'auto';
        }
    }

    // --- opening, choosing, closing ---

    // `scroll` is opt-in, and the mouse never asks for it. A row the pointer
    // is on is by definition already visible, so scrolling to it does nothing
    // useful — and it did something actively harmful: a row only partly in
    // view got scrolled into place, which fired a scroll event, which closed
    // the menu. Moving the pointer up a long list shut it every time, which
    // reads exactly like the menu refusing to be clicked.
    function highlight(row, scroll) {
        if (!open) return;
        if (open.active) open.active.classList.remove('active');
        open.active = row || null;
        if (row) {
            row.classList.add('active');
            // `nearest`, so arrowing through a long list scrolls one row at a
            // time instead of jumping the selected item to the middle.
            if (scroll) row.scrollIntoView({ block: 'nearest' });
        }
    }

    function openMenu(select) {
        closeMenu();
        const menu = buildMenu(select);
        document.body.appendChild(menu);
        open = { select, menu, active: null, typed: '', typedAt: 0 };
        place(menu, select);
        select.setAttribute('aria-expanded', 'true');
        select.classList.add('dd-open');

        const current = menu._rows.find(row => row._option.selected) || menu._rows[0];
        highlight(current);
        // Once the menu is placed and scrollable: a long list opened on its
        // first row, so the model you are already using was somewhere below
        // the fold and looked unselected.
        if (current) current.scrollIntoView({ block: 'nearest' });

        menu.addEventListener('mousedown', event => event.preventDefault());  // keep focus on the select
        menu.addEventListener('mouseover', event => {
            const row = event.target.closest('.dd-item');
            if (row && !row.classList.contains('disabled')) highlight(row);
        });
        menu.addEventListener('click', event => {
            const row = event.target.closest('.dd-item');
            if (row && !row.classList.contains('disabled')) choose(row);
        });
    }

    function choose(row) {
        if (!open) return;
        const select = open.select;
        const option = row._option;
        const changed = option !== select.selectedOptions[0];
        closeMenu();
        if (!changed) return;
        option.selected = true;
        // Both events, in this order, because the app listens for whichever
        // one suited the call site: `onchange="applyProviderPreset()"` in the
        // markup and `addEventListener('input')` in the settings panels. A
        // native popup fires both, so anything less would have looked like
        // the picker silently not saving.
        select.dispatchEvent(new Event('input', { bubbles: true }));
        select.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function closeMenu() {
        if (!open) return;
        open.menu.remove();
        open.select.removeAttribute('aria-expanded');
        open.select.classList.remove('dd-open');
        const select = open.select;
        open = null;
        // Focus goes back to the control, so tabbing carries on from where
        // the menu was rather than from the top of the document.
        try { select.focus({ preventScroll: true }); } catch (e) { select.focus(); }
    }

    // --- what opens it ---

    document.addEventListener('mousedown', event => {
        const select = event.target.closest ? event.target.closest('select') : null;
        if (isUpgradable(select)) {
            // Before the browser can show its own popup. This is also what
            // stops the control taking focus, hence the explicit focus call.
            event.preventDefault();
            if (open && open.select === select) { closeMenu(); return; }
            select.focus();
            openMenu(select);
            return;
        }
        if (open && !event.target.closest('.dd-menu')) closeMenu();
    }, true);

    document.addEventListener('keydown', event => {
        const select = document.activeElement;
        if (!open) {
            if (isUpgradable(select) && (event.key === 'Enter' || event.key === ' '
                                         || (event.key === 'ArrowDown' && event.altKey))) {
                event.preventDefault();
                openMenu(select);
            }
            return;
        }

        const rows = open.menu._rows;
        const index = rows.indexOf(open.active);
        if (event.key === 'Escape') {
            event.preventDefault();
            closeMenu();
        } else if (event.key === 'Enter' || event.key === 'Tab') {
            event.preventDefault();
            if (open.active) choose(open.active); else closeMenu();
        } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            const step = event.key === 'ArrowDown' ? 1 : -1;
            highlight(rows[Math.min(rows.length - 1, Math.max(0, index + step))], true);
        } else if (event.key === 'Home' || event.key === 'End') {
            event.preventDefault();
            highlight(event.key === 'Home' ? rows[0] : rows[rows.length - 1], true);
        } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
            // Type-ahead. Native popups have it, and the list this exists for
            // is a hundred model ids — losing it would make the replacement
            // worse than what it replaced for the one menu that needs it most.
            const now = Date.now();
            open.typed = (now - open.typedAt < 900 ? open.typed : '') + event.key.toLowerCase();
            open.typedAt = now;
            const hit = rows.find(row => row.textContent.trim().toLowerCase().startsWith(open.typed));
            if (hit) highlight(hit, true);
            event.preventDefault();
        }
    }, true);

    // A fixed menu does not travel with a scrolling panel, and one left
    // hanging beside the control it belongs to is worse than one that closed.
    //
    // Somebody else's scroll, though. The menu scrolls itself — on open, to
    // bring the current selection into view, and on every arrow key — and
    // this listener is capturing, so its own scrolling was reaching it and
    // closing it. A hundred-model list was unusable: it shut on the way to
    // whichever option you were reaching for.
    window.addEventListener('scroll', event => {
        const target = event.target;
        // Element check first. A document-level scroll reports the document
        // as its target, and `menu.contains(document)` is false — but the
        // shorter version of this test that passed the menu itself as a
        // fallback returned true for exactly that case, and would have left
        // the menu hanging over a scrolled page.
        if (open && target && target.nodeType === 1 && open.menu.contains(target)) return;
        closeMenu();
    }, true);
    window.addEventListener('resize', () => closeMenu());
    window.addEventListener('blur', () => closeMenu());
})();

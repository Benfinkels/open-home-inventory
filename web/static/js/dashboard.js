/**
 * Open Home Inventory Dashboard Logic
 * Handles client-side API fetching, filtering, KPI calculations, and modal rendering.
 */

let allItems = [];
let activeFilters = {
    search: '',
    category: '',
    status: 'all',
    room: ''
};

document.addEventListener('DOMContentLoaded', () => {
    loadDashboard();
});

async function loadDashboard() {
    try {
        await Promise.all([fetchStats(), fetchItems()]);
    } catch (err) {
        console.error("Failed to load dashboard data:", err);
    }
}

function formatCurrency(val) {
    if (val === null || val === undefined || isNaN(val)) return '$0.00';
    return new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
        minimumFractionDigits: 2
    }).format(val);
}

async function fetchStats() {
    try {
        const res = await fetch('/api/stats');
        if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
        const data = await res.json();

        document.getElementById('kpi-total-val').innerText = formatCurrency(data.total_replacement_cost);
        document.getElementById('kpi-orig-cost').innerText = `Purchase Basis: ${formatCurrency(data.total_purchase_cost)}`;
        document.getElementById('kpi-item-count').innerText = data.total_items;
        document.getElementById('kpi-category-count').innerText = `Across ${Object.keys(data.category_breakdown || {}).length} Categories`;
        document.getElementById('kpi-verified-pct').innerText = `${Math.round(data.verified_percentage || 0)}%`;
        document.getElementById('kpi-verified-ratio').innerText = `${data.verified_items} of ${data.total_items} verified`;
        document.getElementById('kpi-readiness-score').innerText = `${data.audit_readiness_score} / 100`;
        document.getElementById('kpi-serial-ratio').innerText = `${data.items_with_serial} serials recorded`;
    } catch (e) {
        console.warn("Error fetching stats:", e);
    }
}

async function fetchItems() {
    try {
        const res = await fetch('/api/items');
        if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
        allItems = await res.json();
        
        populateFilterDropdowns(allItems);
        renderTable(allItems);
    } catch (e) {
        console.warn("Error fetching items:", e);
    }
}

function populateFilterDropdowns(items) {
    const categorySelect = document.getElementById('filter-category');
    const roomSelect = document.getElementById('filter-room');

    const categories = new Set();
    const rooms = new Set();

    items.forEach(it => {
        if (it.category) categories.add(it.category);
        const rm = it.room || 'General';
        rooms.add(rm);
    });

    const currentCat = categorySelect.value;
    categorySelect.innerHTML = '<option value="">All Categories</option>';
    Array.from(categories).sort().forEach(cat => {
        const opt = document.createElement('option');
        opt.value = cat;
        opt.innerText = cat;
        categorySelect.appendChild(opt);
    });
    categorySelect.value = currentCat;

    const currentRoom = roomSelect.value;
    roomSelect.innerHTML = '<option value="">All Rooms</option>';
    Array.from(rooms).sort().forEach(rm => {
        const opt = document.createElement('option');
        opt.value = rm;
        opt.innerText = rm;
        roomSelect.appendChild(opt);
    });
    roomSelect.value = currentRoom;
}

function handleSearch() {
    activeFilters.search = document.getElementById('filter-search').value.trim().toLowerCase();
    applyFilters();
}

function applyFilters() {
    activeFilters.category = document.getElementById('filter-category').value;
    activeFilters.status = document.getElementById('filter-status').value;
    activeFilters.room = document.getElementById('filter-room').value;

    const filtered = allItems.filter(item => {
        const itemName = item.item_name || item.name || '';
        const itemBrand = item.brand || '';
        const itemModel = item.model_or_identifiers || item.model || '';
        const itemSerial = item.serial_number || '';
        const itemRoom = item.room || 'General';
        const isVerified = Boolean(item.receipt_verified);

        // Search filter
        if (activeFilters.search) {
            const query = activeFilters.search;
            const textMatch = 
                itemName.toLowerCase().includes(query) ||
                itemBrand.toLowerCase().includes(query) ||
                itemModel.toLowerCase().includes(query) ||
                itemSerial.toLowerCase().includes(query) ||
                itemRoom.toLowerCase().includes(query);
            if (!textMatch) return false;
        }

        // Category filter
        if (activeFilters.category && item.category !== activeFilters.category) {
            return false;
        }

        // Room filter
        if (activeFilters.room && itemRoom !== activeFilters.room) {
            return false;
        }

        // Status filter
        if (activeFilters.status === 'verified' && !isVerified) return false;
        if (activeFilters.status === 'unverified' && isVerified) return false;
        if (activeFilters.status === 'has_serial' && (!itemSerial || itemSerial.trim() === '')) return false;

        return true;
    });

    renderTable(filtered);
}

function renderTable(items) {
    const tbody = document.getElementById('inventory-tbody');
    const emptyState = document.getElementById('empty-state');
    const countLabel = document.getElementById('item-showing-count');

    tbody.innerHTML = '';
    countLabel.innerText = `Showing ${items.length} item${items.length === 1 ? '' : 's'}`;

    if (items.length === 0) {
        emptyState.style.display = 'block';
        return;
    }

    emptyState.style.display = 'none';

    items.forEach(item => {
        const tr = document.createElement('tr');
        const itemName = item.item_name || item.name || 'Unnamed Asset';
        const brand = item.brand || 'Generic';
        const model = item.model_or_identifiers || item.model || 'Standard';
        const room = item.room || 'General';
        const isVerified = Boolean(item.receipt_verified);

        // Column 1: Item & Spec
        const specHtml = `
            <div class="item-main">
                <span class="item-name">${escapeHtml(itemName)}</span>
                <span class="item-brand-model">${escapeHtml(brand)} • ${escapeHtml(model)}</span>
            </div>
        `;

        // Column 2: Category & Room
        const catRoomHtml = `
            <div>
                <span class="badge-tag badge-tag-category">${escapeHtml(item.category || 'General')}</span>
                <span class="badge-tag badge-tag-room">${escapeHtml(room)}</span>
            </div>
        `;

        // Column 3: Serial
        let serialHtml = '<span class="text-muted">—</span>';
        if (item.serial_number) {
            serialHtml = `
                <div class="serial-cell">
                    ${isVerified ? '<span class="serial-verified-icon">🔒</span>' : ''}
                    <span>${escapeHtml(item.serial_number)}</span>
                </div>
            `;
        }

        // Column 4 & 5: Costs
        const purchaseCost = formatCurrency(item.purchase_price_usd);
        const replacementVal = formatCurrency(item.estimated_replacement_value_usd);

        // Column 6: Receipt Proof
        let receiptHtml = '';
        if (isVerified) {
            const mLabel = item.merchant || 'Verified Receipt';
            receiptHtml = `<span class="status-badge status-verified">✓ ${escapeHtml(mLabel)}</span>`;
        } else {
            receiptHtml = `<span class="status-badge status-unverified">Visual Estimate</span>`;
        }

        // Column 7: Action
        const actionHtml = `
            <button class="btn-inspect" onclick="inspectItem(${item.id})">Inspect</button>
        `;

        tr.innerHTML = `
            <td>${specHtml}</td>
            <td>${catRoomHtml}</td>
            <td>${serialHtml}</td>
            <td class="mono">${purchaseCost}</td>
            <td class="mono font-semibold">${replacementVal}</td>
            <td>${receiptHtml}</td>
            <td>${actionHtml}</td>
        `;

        tbody.appendChild(tr);
    });
}

function inspectItem(itemId) {
    const item = allItems.find(it => it.id === itemId);
    if (!item) return;

    const modal = document.getElementById('item-modal');
    const modalTitle = document.getElementById('modal-title');
    const modalBody = document.getElementById('modal-body');

    const itemName = item.item_name || item.name || 'Unnamed Asset';
    const brand = item.brand || 'Generic';
    const model = item.model_or_identifiers || item.model || '';

    modalTitle.innerText = `${itemName} (${brand} ${model})`;

    let html = `
        <div class="detail-row">
            <span class="detail-label">Asset ID</span>
            <span class="detail-val mono">#${escapeHtml(item.id)}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Category</span>
            <span class="detail-val">${escapeHtml(item.category || 'Uncategorized')}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Condition</span>
            <span class="detail-val">${escapeHtml(item.condition || 'Good')}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Serial Number</span>
            <span class="detail-val mono">${escapeHtml(item.serial_number || 'None detected')}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Purchase Date & Cost</span>
            <span class="detail-val">${escapeHtml(item.purchase_date || 'Unknown')} • ${formatCurrency(item.purchase_price_usd)}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Current Replacement Value</span>
            <span class="detail-val mono" style="color: var(--accent-emerald);">${formatCurrency(item.estimated_replacement_value_usd)}</span>
        </div>
        <div class="detail-row">
            <span class="detail-label">Receipt Proof Match</span>
            <span class="detail-val">${item.receipt_verified ? `✓ Matched (${escapeHtml(item.merchant || '')} ${escapeHtml(item.order_number || '')})` : 'Visual Estimate only (No matching receipt yet)'}</span>
        </div>
    `;

    if (item.receipt_evidence) {
        html += `
            <div class="detail-row">
                <span class="detail-label">Receipt Evidence</span>
                <span class="detail-val mono" style="font-size: 0.75rem;">${escapeHtml(item.receipt_evidence)}</span>
            </div>
        `;
    }

    if (item.description) {
        html += `
            <div class="detail-row">
                <span class="detail-label">Appraisal Description</span>
                <span class="detail-val" style="font-size: 0.85rem;">${escapeHtml(item.description)}</span>
            </div>
        `;
    }

    modalBody.innerHTML = html;
    modal.style.display = 'flex';
}

function closeModal() {
    document.getElementById('item-modal').style.display = 'none';
}

function closeModalOnBackdrop(e) {
    if (e.target.id === 'item-modal') {
        closeModal();
    }
}

function escapeHtml(text) {
    if (!text) return '';
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

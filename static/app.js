const tg = window.Telegram?.WebApp;
if (tg) {
    tg.ready();
    tg.expand();
    tg.setHeaderColor('#f5f1e8');
    tg.setBackgroundColor('#f5f1e8');
}

function telegramAuthorization() {
    if (!tg?.initData) throw new Error('Откройте этот раздел из Telegram');
    return tg.initData;
}

function showToast(message) {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.classList.remove('hidden');
    clearTimeout(window.__fyvessaToastTimer);
    window.__fyvessaToastTimer = setTimeout(() => toast.classList.add('hidden'), 2400);
}

async function api(url, options = {}) {
    const response = await fetch(url, {
        ...options,
        headers: {
            'Content-Type': 'application/json',
            'Authorization': telegramAuthorization(),
            ...(options.headers || {})
        }
    });
    if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Не удалось выполнить действие');
    }
    return response.json();
}

async function loadProtectedFragments() {
    for (const container of document.querySelectorAll('[data-auth-fragment]')) {
        try {
            const response = await fetch(container.dataset.authFragment, {
                headers: {'Authorization': telegramAuthorization()}
            });
            if (!response.ok) throw new Error('Не удалось подтвердить Telegram-сессию');
            container.innerHTML = await response.text();
        } catch (error) {
            container.innerHTML = `<div class="rounded-[2rem] border border-dashed border-black/20 p-10 text-center"><p class="text-3xl">↗</p><h2 class="mt-3 text-xl font-black">Нужен Telegram</h2><p class="mt-2 text-black/50">${error.message}</p></div>`;
        }
    }
}

async function toggleFavorite(productId, button) {
    try {
        const result = await api(`/api/favorites/${productId}`, {method: 'POST'});
        if (button) button.textContent = result.favorite ? '♥' : '♡';
        showToast(result.favorite ? 'Добавлено в избранное' : 'Удалено из избранного');
        tg?.HapticFeedback?.impactOccurred('light');
    } catch (error) { showToast(error.message); }
}

async function addToCart(productId, quantity = 1) {
    try {
        await api(`/api/cart/${productId}`, {
            method: 'POST', body: JSON.stringify({quantity})
        });
        showToast('Товар добавлен в корзину');
        tg?.HapticFeedback?.notificationOccurred('success');
    } catch (error) { showToast(error.message); }
}

async function removeFromCart(productId) {
    try {
        await api(`/api/cart/${productId}`, {method: 'DELETE'});
        window.location.reload();
    } catch (error) { showToast(error.message); }
}

async function recordProductView(productId) {
    if (!tg?.initData) return;
    try {
        await api(`/api/products/${productId}/view`, {method: 'POST'});
    } catch (_) {}
}

document.addEventListener('submit', async (event) => {
    if (event.target.id !== 'profile-form') return;
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target).entries());
    try {
        await api('/api/profile', {method: 'POST', body: JSON.stringify(data)});
        showToast('Профиль сохранён');
    } catch (error) {
        showToast(error.message);
    }
});

loadProtectedFragments();

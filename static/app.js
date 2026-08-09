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
    window.__fyvessaToastTimer = setTimeout(() => toast.classList.add('hidden'), 2800);
}

function errorMessage(payload) {
    if (typeof payload?.detail === 'string') return payload.detail;
    if (Array.isArray(payload?.detail)) {
        return payload.detail.map(item => item.msg?.replace(/^Value error, /, '') || 'Проверьте данные').join('. ');
    }
    return 'Не удалось выполнить действие';
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
        throw new Error(errorMessage(payload));
    }
    return response.json();
}

async function loadProtectedFragment(container) {
    try {
        const response = await fetch(container.dataset.authFragment, {
            headers: {'Authorization': telegramAuthorization()}
        });
        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(errorMessage(payload));
        }
        container.innerHTML = await response.text();
    } catch (error) {
        container.innerHTML = `<div class="rounded-[2rem] border border-dashed border-black/20 p-10 text-center"><p class="text-3xl">↗</p><h2 class="mt-3 text-xl font-black">Нужен Telegram</h2><p class="mt-2 text-black/50">${error.message}</p></div>`;
    }
}

async function loadProtectedFragments() {
    for (const container of document.querySelectorAll('[data-auth-fragment]')) {
        await loadProtectedFragment(container);
    }
}

async function refreshCart() {
    const container = document.querySelector('[data-auth-fragment="/api/cart"]');
    if (container) await loadProtectedFragment(container);
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
        const result = await api(`/api/cart/${productId}`, {
            method: 'POST', body: JSON.stringify({quantity})
        });
        showToast(`В корзине: ${result.quantity} шт.`);
        tg?.HapticFeedback?.notificationOccurred('success');
    } catch (error) { showToast(error.message); }
}

async function changeCartQuantity(productId, delta, button) {
    const input = button.closest('[data-cart-item]').querySelector('[data-cart-quantity]');
    const quantity = Math.max(1, Math.min(999, Number(input.value) + delta));
    try {
        await api(`/api/cart/${productId}`, {
            method: 'PUT', body: JSON.stringify({quantity})
        });
        await refreshCart();
    } catch (error) { showToast(error.message); }
}

async function removeFromCart(productId) {
    try {
        await api(`/api/cart/${productId}`, {method: 'DELETE'});
        await refreshCart();
        showToast('Товар удалён');
    } catch (error) { showToast(error.message); }
}

async function recordProductView(productId) {
    if (!tg?.initData) return;
    try {
        await api(`/api/products/${productId}/view`, {method: 'POST'});
    } catch (_) {}
}

async function requestAvailability(productId, form) {
    const quantity = Number(new FormData(form).get('quantity'));
    try {
        await api(`/api/availability/${productId}`, {
            method: 'POST', body: JSON.stringify({quantity})
        });
        showToast('Запрос отправлен администратору');
        form.querySelector('button').textContent = 'Запрос отправлен ✓';
        tg?.HapticFeedback?.notificationOccurred('success');
    } catch (error) { showToast(error.message); }
}

async function checkout(form) {
    const data = Object.fromEntries(new FormData(form).entries());
    data.coins_requested = data.coins_requested || '0';
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    button.textContent = 'Создаём заказ…';
    try {
        const result = await api('/api/orders', {
            method: 'POST', body: JSON.stringify(data)
        });
        window.location.href = `/orders/${result.order_id}`;
    } catch (error) {
        showToast(error.message);
        if (error.message.startsWith('Заполните')) {
            setTimeout(() => { window.location.href = '/profile'; }, 1200);
        }
        button.disabled = false;
        button.textContent = 'Оформить заказ';
    }
}

async function reportPayment(orderId, button) {
    button.disabled = true;
    try {
        await api(`/api/orders/${orderId}/report-payment`, {method: 'POST'});
        const container = document.querySelector(`[data-auth-fragment="/api/orders/${orderId}"]`);
        if (container) await loadProtectedFragment(container);
        showToast('Передано администратору на проверку');
    } catch (error) {
        button.disabled = false;
        showToast(error.message);
    }
}

document.addEventListener('submit', async (event) => {
    if (event.target.id === 'profile-form') {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(event.target).entries());
        try {
            await api('/api/profile', {method: 'POST', body: JSON.stringify(data)});
            showToast('Профиль сохранён');
        } catch (error) { showToast(error.message); }
    }
    if (event.target.id === 'checkout-form') {
        event.preventDefault();
        await checkout(event.target);
    }
    if (event.target.matches('[data-availability-form]')) {
        event.preventDefault();
        await requestAvailability(event.target.dataset.productId, event.target);
    }
});

loadProtectedFragments();

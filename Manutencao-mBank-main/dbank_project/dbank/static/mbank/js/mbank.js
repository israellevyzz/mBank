window.mBank = (function () {
  function getCookie(name) {
    const cookies = document.cookie ? document.cookie.split(';') : [];
    for (let i = 0; i < cookies.length; i++) {
      const c = cookies[i].trim();
      if (c.startsWith(name + '=')) {
        return decodeURIComponent(c.substring(name.length + 1));
      }
    }
    return '';
  }

  function uuid4() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      const r = (Math.random() * 16) | 0;
      const v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function formatBRL(value) {
    const num = Number(value || 0);
    if (Number.isNaN(num)) return '0,00';
    return num.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function parseBRL(str) {
    if (typeof str === 'number') return str;
    const cleaned = String(str || '')
      .replace(/R\$/gi, '')
      .replace(/\s+/g, '')
      .replace(/\./g, '')
      .replace(',', '.');
    const num = parseFloat(cleaned);
    return Number.isFinite(num) ? num : NaN;
  }

  function toast(message, type) {
    let stack = document.getElementById('mb-toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.id = 'mb-toast-stack';
      stack.className = 'mb-toast-stack';
      document.body.appendChild(stack);
    }
    const el = document.createElement('div');
    el.className = 'mb-toast' + (type === 'error' ? ' error' : '');
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(function () {
      el.remove();
    }, 4200);
  }

  function extractError(data, fallback) {
    if (!data) return fallback || 'Não foi possível concluir a operação.';
    if (typeof data === 'string') return data;
    if (data.detail) return Array.isArray(data.detail) ? data.detail.join(' ') : String(data.detail);
    const keys = Object.keys(data);
    if (keys.length > 0) {
      const first = data[keys[0]];
      if (Array.isArray(first)) return first.join(' ');
      if (typeof first === 'string') return first;
    }
    return fallback || 'Erro ao processar requisição.';
  }

  async function apiFetch(url, options) {
    const opts = Object.assign({ credentials: 'same-origin', headers: {} }, options || {});
    const method = (opts.method || 'GET').toUpperCase();
    opts.headers = Object.assign({ 'Accept': 'application/json' }, opts.headers);
    if (method !== 'GET' && method !== 'HEAD') {
      opts.headers['X-CSRFToken'] = getCookie('csrftoken');
      if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(opts.body);
      }
    }
    const res = await fetch(url, opts);
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
    if (!res.ok) {
      const err = new Error(extractError(data, 'Erro na operação (' + res.status + ').'));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function copyText(text, successMsg) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast(successMsg || 'Copiado para a área de transferência!');
      }).catch(function () {
        fallbackCopy(text, successMsg);
      });
    } else {
      fallbackCopy(text, successMsg);
    }
  }

  function fallbackCopy(text, successMsg) {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      toast(successMsg || 'Copiado!');
    } catch (_) {
      toast('Não foi possível copiar.', 'error');
    }
    ta.remove();
  }

  function openModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('open');
  }

  function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('open');
  }

  document.addEventListener('click', function (e) {
    if (e.target.classList && e.target.classList.contains('mb-modal-backdrop')) {
      e.target.classList.remove('open');
    }
    const closeBtn = e.target.closest('[data-close-modal]');
    if (closeBtn) {
      closeModal(closeBtn.getAttribute('data-close-modal'));
    }
    const copyTrigger = e.target.closest('[data-copy-text]');
    if (copyTrigger) {
      copyText(copyTrigger.getAttribute('data-copy-text'), copyTrigger.getAttribute('data-copy-msg'));
    }
    const eyeBtn = e.target.closest('[data-toggle-password]');
    if (eyeBtn) {
      const targetId = eyeBtn.getAttribute('data-toggle-password');
      const input = document.getElementById(targetId);
      if (input) {
        input.type = input.type === 'password' ? 'text' : 'password';
      }
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      document.querySelectorAll('.mb-modal-backdrop.open').forEach(function (m) {
        m.classList.remove('open');
      });
    }
  });

  return {
    getCookie: getCookie,
    uuid4: uuid4,
    formatBRL: formatBRL,
    parseBRL: parseBRL,
    toast: toast,
    apiFetch: apiFetch,
    copyText: copyText,
    openModal: openModal,
    closeModal: closeModal
  };
})();

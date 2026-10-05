// ===== SLIDER =====
(function() {
    const slides = document.querySelectorAll('.slider-slide');
    const dots = document.querySelectorAll('.slider-dot');
    let currentIndex = 0;
    let interval;

    function goToSlide(index) {
        slides.forEach(s => s.classList.remove('active'));
        dots.forEach(d => d.classList.remove('active'));
        slides[index].classList.add('active');
        dots[index].classList.add('active');
        currentIndex = index;
    }

    function nextSlide() {
        const next = (currentIndex + 1) % slides.length;
        goToSlide(next);
    }

    function startSlider() {
        interval = setInterval(nextSlide, 4000);
    }

    function stopSlider() {
        clearInterval(interval);
    }

    if (slides.length > 0) {
        dots.forEach((dot, index) => {
            dot.addEventListener('click', function() {
                stopSlider();
                goToSlide(index);
                startSlider();
            });
        });

        const container = document.getElementById('heroSlider');
        if (container) {
            container.addEventListener('mouseenter', stopSlider);
            container.addEventListener('mouseleave', startSlider);
        }

        goToSlide(0);
        startSlider();
    }
})();

// ===== LANDING MOBILE MENU =====
function toggleMobileMenu() {
    const menu = document.getElementById('landingMobileMenu');
    if (menu) menu.classList.toggle('hidden');
}

// ===== LOGIN MODAL =====
function openLoginModal() {
    const modal = document.getElementById('loginModal');
    const card = document.getElementById('loginModalCard');
    if (modal && card) {
        modal.classList.remove('pointer-events-none', 'opacity-0');
        card.classList.remove('scale-95');
        document.body.style.overflow = 'hidden';
    }
}

function closeLoginModal() {
    const modal = document.getElementById('loginModal');
    const card = document.getElementById('loginModalCard');
    if (modal && card) {
        modal.classList.add('pointer-events-none', 'opacity-0');
        card.classList.add('scale-95');
        document.body.style.overflow = 'auto';
    }
}

// ===== HANDLE LOGIN =====
async function handleLogin(event) {
    event.preventDefault();
    const username = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPassword').value;

    if (!username || !password) {
        showToast('Peringatan!', 'Harap isi username dan password');
        return;
    }

    try {
        const response = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            // Kirim "username" (bukan "email") — sesuai yang diharapkan backend
            body: JSON.stringify({ username, password })
        });

        const data = await response.json();
        console.log('[LOGIN]', response.status, data);

        if (data.success) {
            closeLoginModal();
            showToast('Login Berhasil!', 'Selamat datang kembali');
            setTimeout(() => {
                window.location.href = '/dashboard';
            }, 500);
        } else {
            showToast('Login Gagal!', data.message || 'Username atau password salah');
        }
    } catch (error) {
        console.error('[LOGIN ERROR]', error);
        showToast('Error!', 'Tidak dapat menghubungi server');
    }
}

// ===== HANDLE GOOGLE LOGIN =====
async function handleGoogleLogin() {
    // Simulasi login dengan Google
    try {
        const response = await fetch('/api/login', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ 
                email: 'user@gmail.com', 
                password: 'google_auth' 
            })
        });
        
        const data = await response.json();
        
        if (data.success) {
            closeLoginModal();
            showToast('Login Berhasil!', 'Selamat datang kembali, Admin');
            setTimeout(() => {
                window.location.href = '/dashboard';
            }, 500);
        }
    } catch (error) {
        showToast('Error!', 'Terjadi kesalahan pada server');
    }
}

// ===== HANDLE LOGOUT =====
async function handleLogout1() {
    if (confirm('Yakin ingin keluar?')) {
        try {
            const response = await fetch('/api/logout', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            });
            
            const data = await response.json();
            
            if (data.success) {
                showToast('Logout Berhasil!', 'Sampai jumpa kembali');
                setTimeout(() => {
                    window.location.href = '/';
                }, 500);
            }
        } catch (error) {
            showToast('Error!', 'Terjadi kesalahan pada server');
        }
    }
}

// ===== SAVE SETTINGS =====
async function saveSettings(event) {
    event.preventDefault();
    
    const formData = {
        serverName: document.getElementById('serverName')?.value || '',
        serverIP: document.getElementById('serverIP')?.value || '',
        serverPort: document.getElementById('serverPort')?.value || '',
        timezone: document.getElementById('timezone')?.value || '',
        adminEmail: document.getElementById('adminEmail')?.value || '',
        maxUsers: document.getElementById('maxUsers')?.value || '',
        retentionDays: document.getElementById('retentionDays')?.value || '',
    };
    
    try {
        const response = await fetch('/api/save-settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(formData)
        });
        
        const data = await response.json();
        
        if (data.success) {
            showToast('Berhasil!', 'Pengaturan berhasil disimpan');
        } else {
            showToast('Gagal!', data.message || 'Terjadi kesalahan');
        }
    } catch (error) {
        showToast('Error!', 'Terjadi kesalahan pada server');
    }
}

// ===== TOAST =====
/*function showToast(title, sub) {
    const toast = document.getElementById('successToast');
    if (!toast) return;
    
    document.getElementById('toastTitle').textContent = title;
    document.getElementById('toastSub').textContent = sub || '';
    toast.classList.add('show');
    
    setTimeout(() => {
        toast.classList.remove('show');
        document.getElementById('toastTitle').textContent = 'Login Berhasil!';
        document.getElementById('toastSub').textContent = 'Selamat datang kembali, Admin';
    }, 4000);
}*/

// ===== SIDEBAR =====
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');
    if (!sidebar) return;
    
    const isOpen = !sidebar.classList.contains('-translate-x-full');
    if (isOpen) {
        sidebar.classList.add('-translate-x-full');
        if (overlay) overlay.classList.add('hidden');
    } else {
        sidebar.classList.remove('-translate-x-full');
        if (overlay) overlay.classList.remove('hidden');
    }
}

// ===== EVENT LISTENERS =====
document.addEventListener('DOMContentLoaded', function() {
    // Close modal on Escape
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') closeLoginModal();
    });

    // Close sidebar on resize
    window.addEventListener('resize', function() {
        if (window.innerWidth >= 1024) {
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('sidebarOverlay');
            if (sidebar) sidebar.classList.remove('-translate-x-full');
            if (overlay) overlay.classList.add('hidden');
        }
    });
	hideLoader()
});
function showLoader(msg) {
  const el = document.getElementById("loader-msg");
  const loader = document.getElementById("global-loader");
  if (el) el.innerText = msg;
  if (loader) {
    loader.classList.remove("hidden");
    loader.classList.remove("opacity-0");
    loader.classList.remove("pointer-events-none");
  }
}

// Hides loader animation safely and IMMEDIATELY prevents it from blocking pointer/clicks!
function hideLoader() {
  const loader = document.getElementById("global-loader");
  if (loader) {
    loader.classList.add("opacity-0");
    loader.classList.add("pointer-events-none"); // Instant click release!
    setTimeout(() => {
      loader.classList.add("hidden");
    }, 300);
  }
}

// ===== TOAST SYSTEM (baru, sesuai components/toast.html) =====
function _showToast(type, title, message, duration) {
    duration = duration || 4000;
    const cap = type.charAt(0).toUpperCase() + type.slice(1);
    const toast     = document.getElementById('toast' + cap);
    const titleEl   = document.getElementById('toast' + cap + 'Title');
    const msgEl     = document.getElementById('toast' + cap + 'Message');
    const progressEl= document.getElementById('toast' + cap + 'Progress');
    if (!toast) return;

    if (titleEl) titleEl.textContent = title || '';
    if (msgEl)   msgEl.textContent   = message || '';

    // Reset progress bar
    if (progressEl) {
        progressEl.style.transition = 'none';
        progressEl.style.width = '100%';
    }

    // Tampilkan
    toast.classList.remove('hidden', 'translate-x-full', 'opacity-0');
    toast.classList.add('translate-x-0', 'opacity-100');

    // Animasikan progress bar
    requestAnimationFrame(() => {
        if (progressEl) {
            progressEl.style.transition = 'width ' + duration + 'ms linear';
            progressEl.style.width = '0%';
        }
    });

    // Auto-close
    clearTimeout(toast._toastTimer);
    toast._toastTimer = setTimeout(() => closeToast(type), duration);
}

function closeToast(type) {
    const cap = type.charAt(0).toUpperCase() + type.slice(1);
    const toast = document.getElementById('toast' + cap);
    if (!toast) return;
    clearTimeout(toast._toastTimer);
    toast.classList.remove('translate-x-0', 'opacity-100');
    toast.classList.add('translate-x-full', 'opacity-0');
    setTimeout(() => toast.classList.add('hidden'), 500);
}

function showSuccessToast(title, message) { _showToast('success', title, message); }
function showErrorToast(title, message)   { _showToast('error',   title, message); }
function showWarningToast(title, message) { _showToast('warning', title, message); }
function showInfoToast(title, message)    { _showToast('info',    title, message); }

// Backward-compat: fungsi lama showToast(title, sub) → redirect ke success toast
function showToast(title, sub) { showSuccessToast(title, sub); }

if (typeof window.handleLogout !== 'function') {
    window.handleLogout = function() {
        if (confirm('Yakin ingin keluar?')) {
            fetch('/api/logout', { method: 'POST', credentials: 'same-origin' })
                .then(r => r.json())
                .then(d => { if (d.success) window.location.href = '/'; });
        }
    };
}
// ===== EXPOSE FUNCTIONS TO GLOBAL SCOPE =====
window.openLoginModal = openLoginModal;
window.closeLoginModal = closeLoginModal;
window.handleLogin = handleLogin;
window.handleGoogleLogin = handleGoogleLogin;
window.handleLogout = handleLogout;
window.saveSettings = saveSettings;
window.toggleSidebar = toggleSidebar;
window.toggleMobileMenu = toggleMobileMenu;
window.showToast = showToast;
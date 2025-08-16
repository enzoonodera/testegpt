<?php
@session_start();
include('./conexao.php');
?>

<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title><?php echo $nomeSite;?> - Raspadinhas Online</title>
    <meta name="description" content="Raspe e ganhe prêmios incríveis! PIX na conta instantâneo.">
    
    <!-- Preload Critical Resources -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
    
    <!-- Styles -->
    <link rel="stylesheet" href="assets/style/globalStyles.css?v=<?php echo time();?>"/>
    
    <!-- Bootstrap Icons -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css">
    
    <!-- Scripts -->
    <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    <script src="https://cdn.jsdelivr.net/npm/notiflix@3.2.8/dist/notiflix-aio-3.2.8.min.js"></script>
    <link href="https://cdn.jsdelivr.net/npm/notiflix@3.2.8/src/notiflix.min.css" rel="stylesheet">
    
    <!-- Favicon -->
    <link rel="icon" type="image/x-icon" href="assets/images/favicon.ico">
    
    <!-- Open Graph -->
    <meta property="og:title" content="<?php echo $nomeSite;?> - Raspadinhas Online">
    <meta property="og:description" content="Raspe e ganhe prêmios incríveis! PIX na conta instantâneo.">
    <meta property="og:type" content="website">
    <meta property="og:url" content="<?php echo $urlSite;?>">
    
    <style>
        /* Loading Animation */
        /* Solução definitiva para loading spinner fixo */
        .loading-screen {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background: #0a0a0a;
            z-index: 9999;
            transition: opacity 0.5s ease;
            
            /* Centralização perfeita */
            display: grid;
            place-items: center;
        }

        .loading-spinner {
            width: 50px;
            height: 50px;
            position: relative;
            /* Remove todas as propriedades de borda do elemento principal */
        }

        .loading-spinner::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            border: 3px solid rgba(34, 197, 94, 0.3);
            border-top-color: #22c55e;
            border-radius: 50%;
            
            /* Chaves para rotação sem movimento */
            transform-origin: 50% 50%; /* Centro exato */
            animation: spinFixed 1s linear infinite;
            
            /* Força o elemento a manter posição */
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        @keyframes spinFixed {
            from {
                transform: rotate(0deg);
            }
            to {
                transform: rotate(360deg);
            }
        }

        /* Alternativa ainda mais simples usando apenas border-image */
        .loading-spinner-simple {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: conic-gradient(#22c55e, rgba(34, 197, 94, 0.3));
            animation: rotateSimple 1s linear infinite;
            position: relative;
            
            /* Máscara para criar o efeito de spinner */
            mask: radial-gradient(circle at center, transparent 18px, black 21px);
            -webkit-mask: radial-gradient(circle at center, transparent 18px, black 21px);
        }

        @keyframes rotateSimple {
            to {
                transform: rotate(360deg);
            }
        }

        /* Versão com CSS puro - mais moderna */
        .loading-spinner-modern {
            width: 50px;
            height: 50px;
            background: 
                conic-gradient(from 0deg, transparent, #22c55e, transparent),
                conic-gradient(from 180deg, transparent, rgba(34, 197, 94, 0.3), transparent);
            border-radius: 50%;
            animation: rotateModern 1s linear infinite;
            position: relative;
            
            /* Efeito de máscara para criar o anel */
            mask: radial-gradient(circle, transparent 17px, black 20px);
            -webkit-mask: radial-gradient(circle, transparent 17px, black 20px);
        }

        @keyframes rotateModern {
            100% {
                transform: rotate(360deg);
            }
        }

        .hidden {
            opacity: 0;
            pointer-events: none;
        }

        /* Reset completo para garantir que não há interferências */
        .loading-screen * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        
        /* Smooth scroll */
        html {
            scroll-behavior: smooth;
        }
        
        /* Parallax effect */
        .parallax-element {
            transform: translateZ(0);
            will-change: transform;
        }
        
        /* Animations */
        @keyframes fadeInUp {
            from {
                opacity: 0;
                transform: translateY(30px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        .animate-fade-in-up {
            animation: fadeInUp 0.6s ease-out forwards;
        }
        
        /* Floating elements animation */
        .floating {
            animation: floating 3s ease-in-out infinite;
        }
        
        @keyframes floating {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-10px); }
        }
        
        /* Glowing effect */
        .glow {
            box-shadow: 0 0 20px rgba(34, 197, 94, 0.3);
        }
        
        .glow:hover {
            box-shadow: 0 0 30px rgba(34, 197, 94, 0.5);
        }
    </style>
</head>
<body>
    <!-- Loading Screen -->
    <div class="loading-screen" id="loadingScreen">
        <div class="loading-spinner"></div>
    </div>

    <?php include('./inc/header.php'); ?>

    <main>
        <?php include('./components/carrossel.php'); ?>

        <?php include('./components/ganhos.php'); ?>

        <?php include('./components/chamada.php'); ?>
    
        <?php include('./components/modals.php'); ?>
        
        <?php include('./components/testimonials.php'); ?>
    </main>

    <?php include('./inc/footer.php'); ?>

    <script>
        // Loading screen
        window.addEventListener('load', function() {
            const loadingScreen = document.getElementById('loadingScreen');
            setTimeout(() => {
                loadingScreen.classList.add('hidden');
            }, 1000);
        });

        // Smooth animations on scroll
        const observerOptions = {
            threshold: 0.1,
            rootMargin: '0px 0px -50px 0px'
        };

        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('animate-fade-in-up');
                }
            });
        }, observerOptions);

        // Observe elements for animation
        document.addEventListener('DOMContentLoaded', function() {
            const elementsToAnimate = document.querySelectorAll('.step-item, .game-category, .prize-item');
            elementsToAnimate.forEach(el => {
                observer.observe(el);
            });
        });

        // Parallax effect for hero section
        window.addEventListener('scroll', () => {
            const scrolled = window.pageYOffset;
            const heroElements = document.querySelectorAll('.parallax-element');
            
            heroElements.forEach(element => {
                const speed = element.dataset.speed || 0.5;
                element.style.transform = `translateY(${scrolled * speed}px)`;
            });
        });

        // Add floating animation to certain elements
        document.addEventListener('DOMContentLoaded', function() {
            const floatingElements = document.querySelectorAll('.hero-visuals .gaming-item');
            floatingElements.forEach((el, index) => {
                el.style.animationDelay = `${index * 0.5}s`;
                el.classList.add('floating');
            });
        });

        // Notiflix configuration
        Notiflix.Notify.init({
            width: '300px',
            position: 'right-top',
            distance: '20px',
            opacity: 1,
            borderRadius: '12px',
            rtl: false,
            timeout: 4000,
            messageMaxLength: 110,
            backOverlay: false,
            backOverlayColor: 'rgba(0,0,0,0.5)',
            plainText: true,
            showOnlyTheLastOne: false,
            clickToClose: true,
            pauseOnHover: true,
            ID: 'NotiflixNotify',
            className: 'notiflix-notify',
            zindex: 4001,
            fontFamily: 'Inter',
            fontSize: '14px',
            cssAnimation: true,
            cssAnimationDuration: 400,
            cssAnimationStyle: 'zoom',
            closeButton: false,
            useIcon: true,
            useFontAwesome: false,
            fontAwesomeIconStyle: 'basic',
            fontAwesomeIconSize: '16px',
            success: {
                background: '#22c55e',
                textColor: '#fff',
                childClassName: 'notiflix-notify-success',
                notiflixIconColor: 'rgba(0,0,0,0.2)',
                fontAwesomeClassName: 'fas fa-check-circle',
                fontAwesomeIconColor: 'rgba(0,0,0,0.2)',
                backOverlayColor: 'rgba(34,197,94,0.2)',
            }
        });

        // Dynamic copyright year
        document.addEventListener('DOMContentLoaded', function() {
            const currentYear = new Date().getFullYear();
            const copyrightElements = document.querySelectorAll('.footer-description');
            if (copyrightElements.length > 0) {
                copyrightElements[0].innerHTML = copyrightElements[0].innerHTML.replace('2025', currentYear);
            }
        });

        // Add glow effect to interactive elements
        document.addEventListener('DOMContentLoaded', function() {
            const glowElements = document.querySelectorAll('.btn-register, .hero-cta, .game-btn');
            glowElements.forEach(el => {
                el.classList.add('glow');
            });
        });

        // Mobile menu toggle (if needed)
        function toggleMobileMenu() {
            const mobileMenu = document.querySelector('.mobile-menu');
            if (mobileMenu) {
                mobileMenu.classList.toggle('active');
            }
        }

        // Console welcome message
        console.log('%c🎯 RaspaGreen - Bem-vindo!', 'color: #22c55e; font-size: 16px; font-weight: bold;');
        console.log('%cSistema carregado com sucesso!', 'color: #16a34a; font-size: 12px;');
    </script>

    <!-- Performance and Analytics -->
    <script>
        // Performance monitoring
        window.addEventListener('load', function() {
            if ('performance' in window) {
                const loadTime = performance.timing.loadEventEnd - performance.timing.navigationStart;
                console.log(`Página carregada em ${loadTime}ms`);
            }
        });

        // Error handling
        window.addEventListener('error', function(e) {
            console.error('Erro na página:', e.error);
        });

        // Lazy loading for images when implemented
        if ('IntersectionObserver' in window) {
            const imageObserver = new IntersectionObserver((entries, observer) => {
                entries.forEach(entry => {
                    if (entry.isIntersecting) {
                        const img = entry.target;
                        img.src = img.dataset.src;
                        img.classList.remove('lazy');
                        imageObserver.unobserve(img);
                    }
                });
            });

            document.querySelectorAll('img[data-src]').forEach(img => {
                imageObserver.observe(img);
            });
        }
    </script>
</body>
</html>
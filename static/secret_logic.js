document.addEventListener('DOMContentLoaded', () => {
    let state = 0;
    let lastClickTimestamp = 0;
    let clickerTimestamps = [];
    let cpsUpdateInterval = null;
    let timerInterval = null;
    let timerElement = null;

    function startTimer(durationSeconds) {
        stopTimer();

        timerElement = document.createElement('div');
        Object.assign(timerElement.style, {
            position: 'fixed', top: '10px', left: '50%', transform: 'translateX(-50%)',
            padding: '5px 15px', background: 'rgba(255, 255, 255, 0.1)',
            borderRadius: '20px', color: '#fff', fontFamily: 'monospace',
            fontSize: '1.2em', zIndex: '10001', userSelect: 'none', transition: 'opacity 0.3s'
        });
        document.body.appendChild(timerElement);

        const startTime = Date.now();

        timerInterval = setInterval(() => {
            const elapsedTime = (Date.now() - startTime) / 1000;
            if (elapsedTime >= durationSeconds) {
                stopTimer();
            } else {
                timerElement.textContent = elapsedTime.toFixed(1);
            }
        }, 100);
    }

    function stopTimer() {
        clearInterval(timerInterval);
        if (timerElement) {
            timerElement.remove();
            timerElement = null;
        }
    }


    function createTriggerZone(id, styles) {
        const zone = document.createElement('div');
        zone.id = id;
        Object.assign(zone.style, { position: 'fixed', cursor: 'pointer', zIndex: '10000' }, styles);
        return zone;
    }

    const zones = {
        zone1: createTriggerZone('zone1', { bottom: '0', left: '0', width: '30px', height: '30px' }),
        zone2: createTriggerZone('zone2', { top: '0', right: '0', width: '30px', height: '30px' }),
        zone3: createTriggerZone('zone3', { top: '0', left: '0', width: '50px', height: '50px' })
    };

    function resetSequence(message) {
        // console.warn(message || "ПОСЛЕДОВАТЕЛЬНОСТЬ ПРОВАЛЕНА.");
        clearInterval(cpsUpdateInterval);
        stopTimer();
        window.location.reload();
    }
    
    zones.zone1.addEventListener('click', () => {
        if (state === 0) {
            state = 1; lastClickTimestamp = Date.now();
            // console.log("Шаг 1: Принят.");
            startTimer(3.5);
            zones.zone1.remove(); document.body.appendChild(zones.zone2);
        }
    });

    zones.zone2.addEventListener('click', () => {
        if (state === 1) {
            const timeDiff = Date.now() - lastClickTimestamp;
            if (timeDiff >= 2000 && timeDiff <= 3500) {
                state = 2; lastClickTimestamp = Date.now();
                // console.log(`Шаг 2: Верный тайминг (${timeDiff}ms).`);
                startTimer(4.5); 
                zones.zone2.remove(); document.body.appendChild(zones.zone3);
            } else {
                resetSequence(`ПРОВАЛ. Неверный тайминг: ${timeDiff}ms.`);
            }
        }
    });
    
    zones.zone3.addEventListener('click', () => {
        const now = Date.now();

        if (state === 2) {
            const timeDiff = now - lastClickTimestamp;
            if (timeDiff >= 3000 && timeDiff <= 4500) {
                state = 3; lastClickTimestamp = now;
                // console.log(`Шаг 3: Первый слепой клик принят.`);
                startTimer(3.0);
            } else {
                resetSequence(`ПРОВАЛ. Тайминг для слепого клика: ${timeDiff}ms.`);
            }
            return;
        }

        if (state === 3) {
            const timeDiff = now - lastClickTimestamp;
            if (timeDiff >= 2000 && timeDiff <= 3000) {
                state = 4;
                // console.log(`Шаг 4: АКТИВАЦИЯ. У вас 3 секунды на 180 кликов.`);
                stopTimer(); 
                const z3 = zones.zone3;
                Object.assign(z3.style, {
                    border: '2px solid #e94560', borderRadius: '50%', background: '#16213e',
                    textAlign: 'center', lineHeight: '46px', fontFamily: 'monospace',
                    fontSize: '18px', color: '#e94560', userSelect: 'none'
                });
                z3.textContent = 'START';
            } else {
                resetSequence(`ПРОВАЛ. Тайминг между слепыми кликами: ${timeDiff}ms.`);
            }
            return;
        }
        
        if (state === 4 || state === 5) {
            const z3 = zones.zone3;
            if (state === 4) {
                state = 5;
                clickerTimestamps = [];
                startTimer(3.0);
                
                setTimeout(() => {
                    if (state === 5) {
                        const totalClicks = clickerTimestamps.length;
                        if (totalClicks >= 180) {
                            state = 6; clearInterval(cpsUpdateInterval); stopTimer();
                            z3.style.background = '#28a745'; z3.style.borderColor = '#28a745'; z3.textContent = 'OK';
                            fetch('/_s_a_p_').then(r => r.json()).then(d => { if(d.path) window.location.href = d.path; })
                                .catch(err => { console.error("Ошибка:", err); resetSequence("Ошибка связи."); });
                        } else { resetSequence(`ПРОВАЛ. Всего ${totalClicks}/180 кликов за 3 секунды.`); }
                    }
                }, 3000);

                cpsUpdateInterval = setInterval(() => {
                    const currentCPS = clickerTimestamps.filter(ts => Date.now() - ts < 1000).length;
                    z3.textContent = currentCPS;
                }, 100);
            }
            if (state === 5) {
                clickerTimestamps.push(now);
            }
        }
    });

    document.body.appendChild(zones.zone1);
});
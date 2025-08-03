document.addEventListener('DOMContentLoaded', () => {
    const serverListContainer = document.getElementById('server-list-container');
    const downtimeTableContainer = document.getElementById('downtime-table-container');

    if (!serverListContainer || !downtimeTableContainer) {
        console.error("Не найдены контейнеры для динамического обновления.");
        return;
    }

    async function updateContent() {
        console.log("Получение обновлений...");
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
            const headers = {
                'X-CSRFToken': csrfToken
            };

            const [serverListResponse, downtimeTableResponse] = await Promise.all([
                fetch('/_get_server_list', { headers: headers }),
                fetch('/_get_downtime_table', { headers: headers })
            ]);

            if (!serverListResponse.ok || !downtimeTableResponse.ok) {
                throw new Error("Ошибка сети при получении данных.");
            }
            
            const serverListHtml = await serverListResponse.text();
            const downtimeTableHtml = await downtimeTableResponse.text();

            serverListContainer.style.opacity = 0;
            downtimeTableContainer.style.opacity = 0;

            setTimeout(() => {
                serverListContainer.innerHTML = serverListHtml;
                downtimeTableContainer.innerHTML = downtimeTableHtml;
                serverListContainer.style.opacity = 1;
                downtimeTableContainer.style.opacity = 1;
            }, 300);

        } catch (error) {
            console.error("Не удалось обновить данные:", error);
        }
    }

    const eventSource = new EventSource("/stream-updates");

    eventSource.onmessage = function(event) {
        if (event.data === "update") {
            updateContent();
        }
    };

    eventSource.onerror = function(err) {
        if (eventSource.readyState === EventSource.CLOSED) {
            return;
        }
        console.error("Ошибка EventSource. Соединение будет закрыто и переустановлено браузером.", err);
        eventSource.close();
    };
});
# Связность между серверами

Каждый сервер проверяет свой список назначений. Blackbox Exporter выполняет TCP connect или HTTPS-запрос, Prometheus Agent отправляет результат в центральный Prometheus. Надпись «источник → цель» относится к направлению пробы. Для встречного измерения добавьте обратную цель на втором сервере.

## Панель

Из каталога `contrib/monitoring` создайте конфигурацию окружения. Значения ниже предполагают, что сертификаты вашего CA уже выпущены. Сертификат сервера должен содержать DNS-имя приёмника в SAN. Разрешите UID 101 чтение серверного ключа и UID 472 чтение файла пароля Grafana с учётом user namespace Docker.

```sh
export GRAFANA_PASSWORD_FILE=/etc/anet/grafana-password
export RECEIVER_TLS_DIR=/etc/anet/monitoring/server-tls
export RECEIVER_BIND=10.0.0.1
docker compose -f compose.yaml -f compose.receiver.yaml up -d
```

В RECEIVER_TLS_DIR положите ca.crt, server.crt и server.key. Приёмник на порту 9443 принимает POST /api/v1/write от узлов с клиентским сертификатом доверенного CA. Используйте выделенный CA для агентов. Подключение к Grafana доступно на 127.0.0.1:3001, к Prometheus на 127.0.0.1:9090. Из удалённого браузера используйте SSH forwarding или HTTPS-прокси оператора. Дашборд доступен по пути /d/anet-server-links. Для кнопки на странице обзора WebUI задайте полный адрес дашборда через VITE_ANET_GRAFANA_URL при сборке WebUI.

### Граф в WebUI

Для панели и центра мониторинга на одном Docker-хосте добавьте `-f compose.panel.yaml` при запуске мониторинга, затем запустите установщик панели с `--profile webui --metrics-panel`. В Ansible этому флагу соответствует `anet_metrics_panel: true`. Оба проекта должны использовать один Docker daemon и одно значение `ANET_METRICS_NETWORK`. Prometheus и auth подключатся к общей внутренней сети. Для отдельно развёрнутого центра задайте доступный из auth частный адрес Prometheus через `ANET_PROMETHEUS_URL` в auth.env. Доступ к графу требует сеанса администратора.

На странице обзора цвет стрелки показывает состояние направленной проверки из опубликованной ревизии. После 45 секунд без пробы стрелка становится серой. Время последнего наблюдения доступно за последний час. Для HTTPS скорость анимации зависит от суммарного размера распакованных тел ответов проб за минуту, поделённого на 60 секунд. Метрика относится к контрольным запросам. Для TCP отображается только состояние. Ссылка в истории Grafana фильтрует данные по источнику, назначению, модулю и адресу.

## Узел наблюдения

Скопируйте agent.example.yml в конфигурацию узла. Установите уникальный source_node и адрес приёмника. В AGENT_TLS_DIR поместите ca.crt, client.crt и client.key для этого наблюдателя, доступные UID 65534. Создайте targets.yml внутри TARGETS_DIR, например:

```yaml
- targets: ["exit.example.net:443"]
  labels: {target_node: exit-1, module: tcp_connect}
- targets: ["https://exit.example.net/ready"]
  labels: {target_node: exit-1, module: https_ready}
```

```sh
export AGENT_CONFIG=/etc/anet/monitoring/agent.yml
export AGENT_TLS_DIR=/etc/anet/monitoring/client-tls
export TARGETS_DIR=/var/lib/anet/probe-targets
docker compose -f compose.probe.yaml up -d
```

Поставляемый targets.yml содержит пустой список. Для изменения списка атомарно замените файл внутри каталога или передайте обновление через управление ревизиями. Оба сервиса наблюдателя используют только исходящие соединения. Временный разрыв с панелью обрабатывает WAL Prometheus Agent в томе queue.

## Проверка

На одном разрешённом тестовом узле укажите открытый и закрытый TCP-порты. В дашборде по ним должны появиться соответственно 1 и 0, а в /alerts центрального Prometheus — ServerLinkUnavailable для закрытого порта после трёх неуспешных проб. После остановки агента через 45 секунд последнее значение исчезает из оперативной панели, для ранее наблюдавшейся связи срабатывает ServerLinkTelemetryMissing (окно обнаружения 1 час). Опрос идёт каждые 10 секунд, длительность TCP-пробы включает установление соединения. Потери контрольных проб и их длительность характеризуют только выбранный модуль. Сквозная передача через VPN, скорость передачи данных и причина сетевого ограничения требуют отдельных измерений.

Редактор доступен на странице серверов через действие «Проверки связности». Выберите целевой сервер, модуль и конечный адрес из списка адресов, разрешённых на наблюдателе. Предпросмотр показывает добавленные и удалённые проверки. Публикация использует номер базовой ревизии, конфликт сохраняет черновик до решения оператора. Кнопка «Обновить статус» читает номер применённой ревизии и ответ узла.

Действие «Экспорт inventory» выгружает адреса узлов и опубликованные наборы проб в YAML-совместимом JSON. Перед запуском Ansible задайте anet_mode, anet_profile и SSH-пользователя через host_vars или group_vars. Экспорт не включает пароли и ключи.

Для отправки уведомлений скопируйте alertmanager.example.yml в закрытый конфиг, задайте webhook оператора и экспортируйте его путь в ALERTMANAGER_CONFIG. Добавьте `-f compose.alerts.yaml` к команде запуска центра. Alertmanager группирует события по узлу наблюдения, цели и модулю, отправляет сообщение о восстановлении и сохраняет состояние уведомлений в отдельном томе.

Оповещение `ServerLinkTelemetryMissing` следит за направлениями, для которых Prometheus получал пробы в последний час. Если тишина продолжается весь час, направление выходит из этого окна и Alertmanager может прислать `resolved`. Для подтверждения восстановления проверьте время новой пробы и её результат на графе. Подтверждённая узлом ревизия означает, что узел записал цели проверок.

## Controlled HTTPS transfer

The optional `https_capacity` module downloads an uncompressed 65,536-byte file from an explicitly enrolled peer. Select the registered destination in the panel, specify the exact HTTPS URL ending in `/anet/capacity.bin`, and add that complete URL to the source node's local target allowlist. The signed source-node revision admits at most two capacity edges. A query string, fragment, redirect, non-HTTPS target, oversized response, wrong marker, failed certificate check or non-200 status prevents a successful measurement.

On the destination, provision a dedicated server certificate for its advertised address and a CA for the permitted probe clients. Set CAPACITY_SERVER_TLS_DIR to a directory containing server.crt, server.key and ca.crt readable by the container user. Create the payload once in a private deployment directory:

```sh
head -c 65536 /dev/urandom > /private/anet-capacity.bin
export CAPACITY_PAYLOAD=/private/anet-capacity.bin
export CAPACITY_SERVER_TLS_DIR=/private/capacity-server
export CAPACITY_BIND=192.0.2.10 CAPACITY_PORT=9444
docker compose -f contrib/monitoring/compose.capacity-target.yaml up -d
```

Use the operator-assigned address instead of the documentation address. Limit reachability to the intended sources. The endpoint requires a client certificate, serves one GET path, disables ranges and compression, and rate-limits each certificate to six requests per minute with one burst. A second limit bounds aggregate requests. Dedicate a distinct client certificate to each source node. Mount just ca.crt, client.crt and client.key via CAPACITY_CLIENT_TLS_DIR; ca.crt there trusts the intended server certificate. Keep CA private keys off both containers. In the full installer use `--capacity-probes` (Ansible `anet_capacity_probes: true`). For a separate probe agent, add `-f contrib/monitoring/compose.capacity-source.yaml` to its Compose invocation. The module's credential is optional for existing TCP and readiness probes.

At a 10-second interval, one enabled direction transfers up to 540 MiB of payload per day, plus TLS and protocol overhead. Disable the edge to stop its transfers. The recording rule `anet_link_capacity_mbps` divides 65,536 × 8 bits by elapsed probe seconds and 1,000,000. It includes DNS, TCP, TLS and response time. It describes small-object HTTPS download goodput from destination to source. Uplink capacity, bulk throughput, and other transports may differ. Bind a speed-policy route to this module only when its source network namespace and destination endpoint traverse the path the operator wants to compare. The panel shows the measured Mbit/s. The speed policy uses three consecutive comparison windows and a 60-second hold.

The local check `python3 contrib/monitoring/test_capacity.py` uses the pinned images in the Docker cache and an isolated network to test the authenticated transfer, rejection cases and Prometheus rules. The exporter reads the complete body and exposes the observed byte count. [Blackbox exporter HTTP implementation](https://github.com/prometheus/blackbox_exporter/blob/v0.27.0/prober/http.go)

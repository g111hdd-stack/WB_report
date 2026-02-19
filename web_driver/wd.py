import os
import time
import random
import zipfile
import datetime
import requests
import pandas as pd

from typing import Type, Optional, Tuple
from functools import wraps
from sqlalchemy.exc import IntegrityError

from config import TOKEN, CHAT_ID
from database.models import Market
from database.db import DbConnection
from log_api import logger, get_moscow_time
from database.data_classes import DataWBReportDaily

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PwTimeoutError,
    Error as PwError,
)

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

TIME_AWAITED = 25
TIME_SLEEP = (10, 15)

URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def request_telegram(mes: str, disable_notification: bool = False):
    response = requests.post(
        URL,
        data={
            "chat_id": CHAT_ID,
            "text": mes,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        },
    )
    for _ in range(3):
        if response.status_code == 200:
            break
        time.sleep(20)
    else:
        logger.error(f"Неудалось отправить сообщение: {mes}")
        logger.error(f"Ошибка {response.status_code}")


def handle_exceptions(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Ошибка при выполнении функции '{func.__name__}': {e}")
    return wrapper


def _parse_proxy(proxy: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    В твоём selenium-коде прокси приходил строкой.
    Ожидаемый формат: 'http://login:pass@host:port' (или без логина/пароля).

    Playwright хочет:
      proxy={"server":"http://host:port","username":"login","password":"pass"}
    """
    if not proxy:
        return "", None, None

    if "@" not in proxy:
        # уже может быть 'http://host:port' или 'socks5://host:port'
        return proxy, None, None

    creds, hostport = proxy.split("@", 1)

    scheme = "http"
    rest = creds
    if "://" in creds:
        scheme, rest = creds.split("://", 1)

    if ":" in rest:
        user, pwd = rest.split(":", 1)
    else:
        user, pwd = rest, ""

    server = f"{scheme}://{hostport}"
    return server, user, pwd


def modal_exceptions(func):
    """
    В Selenium ловили ElementClickInterceptedException.
    В Playwright похожие ситуации проявляются как TimeoutError/Playwright Error при клике.

    Поведение сохраняем:
    - пробуем выполнить функцию
    - если похоже на модалку/перехват, пытаемся нажать "отмена/закрыть"
    - повторяем функцию
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except (PwTimeoutError, PwError) as e:
            logger.warning(f"Возможная модалка/перехват клика, пробую закрыть. Причина: {e}")
            try:
                cancel_btn = self.page.locator(
                    "button.zYbWaxtcLWbZ0k3fKPTi.llfYEylHL4V2OpZmoDqx"
                )
                cancel_btn.wait_for(state="visible", timeout=TIME_AWAITED * 1000)
                cancel_btn.click(timeout=TIME_AWAITED * 1000)
                time.sleep(random.randint(*TIME_SLEEP))
            except Exception:
                logger.error("Не удалось закрыть модальное окно.")
            return func(self, *args, **kwargs)
    return wrapper


class BrowserController:
    """
    Playwright Chromium controller, интерфейс совместим со старым Selenium WebDriver:
      - load_url(url)
      - is_browser_active()
      - stores_report_daily()
      - quit()
    """

    def __init__(self, market: Type[Market], user: str, db_conn_admin: DbConnection, db_conn_arris: DbConnection):
        # --- Данные/контекст как было ---
        self.user = user
        self.market = market
        self.new_path = None
        self.client_id = market.client_id
        self.db_conn_admin = db_conn_admin
        self.db_conn_arris = db_conn_arris

        self.proxy = market.connect_info.proxy
        self.phone = market.connect_info.phone
        self.browser_id = f"{market.connect_info.phone}_WB"
        self.marketplace = self.db_conn_admin.get_marketplace()

        self.alerts = {"Штраф": {}}

        # --- Пути как было ---
        self.reports_path = os.path.join(os.getcwd(), "reports")
        self.profile_path = os.path.join(os.getcwd(), "profile", self.browser_id)

        os.makedirs(self.profile_path, exist_ok=True)
        os.makedirs(self.reports_path, exist_ok=True)

        # --- Playwright start ---
        self._pw = sync_playwright().start()

        # Proxy (нативно)
        server, username, password = _parse_proxy(self.proxy)
        proxy_cfg = None
        if server:
            proxy_cfg = {"server": server}
            if username is not None:
                proxy_cfg["username"] = username
                proxy_cfg["password"] = password or ""

        # Небольшая JS-маскировка (можно убрать, если не нужно)
        # stealth_js = r"""
        # try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e) {}
        # try { if (!window.chrome) Object.defineProperty(window, 'chrome', { value: { runtime: {} } }); } catch(e) {}
        # try { Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU','ru'] }); } catch(e) {}
        # try { Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] }); } catch(e) {}
        # """.strip()

        # Persistent context = профиль на диск (аналог -profile в Firefox)
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_path,
            headless=False,
            proxy=proxy_cfg,
            locale="ru-RU",
            no_viewport=True,  # ближе к реальному браузеру
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,1200",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        # self.context.add_init_script(stealth_js)

        # timeouts (НЕ бесконечные, чтобы не зависать навсегда)
        self.context.set_default_timeout(60_000)
        self.context.set_default_navigation_timeout(120_000)

        # page
        pages = self.context.pages
        self.page = pages[0] if pages else self.context.new_page()

        # как в твоём async примере — лёгкий "прогрев"
        try:
            self.page.goto("https://www.wildberries.ru", wait_until="load")
        except Exception:
            pass

    # --- Вспомогательные методы ---
    def _sleep_rand(self):
        time.sleep(random.randint(*TIME_SLEEP))

    # --- Логика авторизации ---
    def check_auth(self):
        try:
            self.page.wait_for_load_state("load", timeout=TIME_AWAITED * 4 * 1000)

            last_url = None
            for _ in range(6):
                cur_url = self.page.url
                if last_url == cur_url:
                    break
                last_url = cur_url
                self.page.wait_for_load_state("load", timeout=TIME_AWAITED * 4 * 1000)
                self._sleep_rand()
            else:
                raise Exception("Превышено время загрузки страницы")

            if self.marketplace.link in (last_url or ""):
                logger.info(f"Автоматизация {self.market.name_company} запущена")
                self.wb_auth(self.marketplace)

            if self.marketplace.domain in (last_url or ""):
                logger.info(f"Вход в ЛК {self.market.name_company} выполнен")

        except Exception as e:
            self.quit(f"Ошибка автоматизации. {str(e).splitlines()[0]}")

    def wb_auth(self, marketplace):
        logger.info(f"Ввод номера {self.phone}")

        # 1) Ввод телефона и поиск кнопки отправки
        for _ in range(3):
            try:
                self._sleep_rand()
                input_phone = self.page.locator("[data-testid='phone-input']")
                input_phone.wait_for(state="visible", timeout=TIME_AWAITED * 4 * 1000)
                input_phone.click()
                input_phone.fill(self.phone)

                self._sleep_rand()
                button_phone = self.page.locator('xpath=//*[@data-testid="submit-phone-button"]')
                button_phone.wait_for(state="visible", timeout=TIME_AWAITED * 4 * 1000)
                break
            except PwTimeoutError:
                self.page.reload(wait_until="load")
        else:
            raise Exception("Страница не получена")

        logger.info(f"Проверка заявки на СМС на номер {self.phone}")

        # 2) Фиксируем момент запроса
        time_request = get_moscow_time()
        self.db_conn_admin.check_phone_message(user=self.user, phone=self.phone, time_request=time_request)

        # 3) Нажимаем кнопку "получить код"
        button_phone.click()

        logger.info(f"Ожидание кода на номер {self.phone}")

        # 4) Записываем запрос СМС в БД (чтобы не было гонки)
        for _ in range(3):
            try:
                self.db_conn_admin.add_phone_message(
                    user=self.user,
                    phone=self.phone,
                    marketplace=marketplace.marketplace,
                    time_request=time_request,
                )
                break
            except IntegrityError:
                self._sleep_rand()
        else:
            raise Exception("Ошибка параллельных запросов")

        # 5) Забираем код из БД
        mes = self.db_conn_admin.get_phone_message(
            user=self.user,
            phone=self.phone,
            marketplace=marketplace.marketplace,
        )

        logger.info(f"Код на номер {self.phone} получен: {mes}")
        logger.info(f"Ввод кода {mes}")

        # 6) Ввод кода
        try:
            self._sleep_rand()
            inputs_code = self.page.locator(".InputCell-PB5beCCt55")
            inputs_code.first.wait_for(state="visible", timeout=TIME_AWAITED * 4 * 1000)

            count = inputs_code.count()
            if len(mes) == count:
                for i in range(count):
                    inputs_code.nth(i).click()
                    inputs_code.nth(i).fill(mes[i])
            else:
                raise Exception("Ошибка ввода кода")
        except PwTimeoutError:
            raise Exception("Отсутствует поле ввода кода")

        # 7) Ждём вход
        logger.info(f"Вход в ЛК {marketplace.marketplace} {self.market.name_company}")
        for _ in range(10):
            if marketplace.domain in (self.page.url or ""):
                logger.info(f"Вход в ЛК {marketplace.marketplace} {self.market.name_company} выполнен")
                return
            self._sleep_rand()

    # --- API как в main.py ---
    def is_browser_active(self):
        try:
            return (self.page is not None) and (not self.page.is_closed()) and bool(self.page.url)
        except Exception:
            return False

    def load_url(self, url: str):
        if self.client_id is None:
            self.quit(f"{self.market.name_company} {self.market.entrepreneur} не обнаружен в client_id")
            return

        logger.info(f"Авторизация {self.market.name_company}")
        self.page.goto(url, wait_until="load", timeout=TIME_AWAITED * 4 * 1000)
        self.check_auth()

    def quit(self, text: str = None):
        if text:
            logger.error(f"{text}")
        else:
            logger.info(f"Браузер для {self.market.name_company} закрыт")

        try:
            if getattr(self, "context", None):
                self.context.close()
        finally:
            try:
                if getattr(self, "_pw", None):
                    self._pw.stop()
            except Exception:
                pass

    # --- Сбор/скачивание отчётов ---
    @modal_exceptions
    def stores_report_daily(self) -> None:
        """Собирает список отчётов."""
        logger.info(f"Сбор доступных отчётов {self.market.name_company}.")
        reports = {}

        # 1) Открыть страницу отчётов и дождаться таблицу
        for _ in range(5):
            self.page.goto(
                "https://seller.wildberries.ru/suppliers-mutual-settlements/reports-implementations/reports-daily",
                wait_until="load",
                timeout=TIME_AWAITED * 4 * 1000,
            )
            self._sleep_rand()
            self.page.reload(wait_until="load")
            self._sleep_rand()

            try:
                rows = self.page.locator(".Reports-table-row__Z2QO2UwUMF")
                rows.first.wait_for(state="visible", timeout=TIME_AWAITED * 1000)
                if rows.count() == 0:
                    raise PwTimeoutError("Нет строк отчётов")
                break
            except PwTimeoutError:
                continue
        else:
            logger.info(f"Нет отчётов {self.market.name_company}.")
            return

        # 2) Собрать отчёты и отфильтровать уже загруженные
        rows = self.page.locator(".Reports-table-row__Z2QO2UwUMF")
        existing_ids = set(self.db_conn_arris.get_reports_id(client_id=self.client_id))

        for i in range(rows.count()):
            element = rows.nth(i)
            try:
                spans = element.locator("span")
                date_text = spans.nth(2).inner_text().strip()
                date_create = datetime.datetime.strptime(date_text, "%d.%m.%Y").date()

                id_report = element.locator('button[data-name="Chips"] div').nth(0).inner_text().strip()

                if id_report not in existing_ids:
                    reports.setdefault(date_create, [])
                    reports[date_create].append(id_report)

            except (ValueError, IndexError, PwError) as e:
                logger.error(f"Ошибка при обработке элемента: {e}")
                continue

        if not reports:
            logger.info(f"Нет новых отчётов {self.market.name_company}.")
            return

        # 3) Скачать новые отчёты
        for date, reports_ids in reports.items():
            self.change_path_downloads(date=date.isoformat())

            for report_id in reports_ids:
                for retry in range(1, 4):
                    if retry != 1:
                        logger.info(f"Повторяем. Осталось {3 - retry} попыток")
                    try:
                        self.page.goto(
                            "https://seller.wildberries.ru/suppliers-mutual-settlements/reports-implementations/"
                            f"reports-daily/report/{report_id}?isGlobalBalance=false",
                            wait_until="load",
                            timeout=TIME_AWAITED * 4 * 1000,
                        )
                        self._sleep_rand()
                        self.download_report_daily(report_id)
                        break
                    except Exception as e:
                        logger.error(f"{e}")
                        continue
                else:
                    logger.error(f"Попытки исчерпаны отчёт {report_id} скачать не удалось")

            # 4) После скачивания за дату — парсим и сохраняем
            self.save_data_in_database(date=date)

    @modal_exceptions
    def download_report_daily(self, report: str) -> None:
        """Скачивание ежедневного отчёта (ZIP) через Playwright download API."""
        for retry in range(6):
            try:
                # в selenium-коде была "первая попытка = TimeoutException" (прогрев).
                # Сохраняем поведение: первая итерация принудительно считается неудачной.
                if retry == 0:
                    raise PwTimeoutError("Первый прогон (искусственный таймаут)")

                btn = self.page.locator("//button[.//span[text()='Скачать Excel']]")
                btn.wait_for(state="visible", timeout=TIME_AWAITED * 1000)

                logger.info(f"Загрузка файла {self.new_path}\\{report} начата.")
                self._sleep_rand()

                with self.page.expect_download(timeout=TIME_AWAITED * 1000) as dl_info:
                    btn.click(timeout=TIME_AWAITED * 1000)

                download = dl_info.value

                suggested = download.suggested_filename or f"{report}.zip"
                dest_path = os.path.join(self.new_path, suggested)

                download.save_as(dest_path)

                logger.info(f"Загрузка файла {self.new_path}\\{report} завершена.")
                return

            except (PwTimeoutError, Exception) as e:
                self._sleep_rand()
                if retry == 5:
                    raise Exception(f"Загрузка файла {self.new_path}\\{report} не удалась. {e}")

        raise Exception(f"Загрузка файла {self.new_path}\\{report} не удалась.")

    def change_path_downloads(self, date: str) -> None:
        """Устанавливанет место скачивания файла."""
        self.new_path = f"{self.reports_path}\\{date}\\{self.client_id}"
        if not os.path.exists(self.new_path):
            os.makedirs(self.new_path)

    # --- Excel/БД/Алерты (не зависят от Selenium/Playwright) ---
    @staticmethod
    def excel_to_entry(excel_file: pd.ExcelFile, realizationreport_id: str, date: datetime.date) -> list[DataWBReportDaily]:
        sheet_name = excel_file.sheet_names[0]
        df = pd.read_excel(excel_file, sheet_name=sheet_name, na_values=["", "NaN"], dtype=str)
        df = df.fillna("")

        entry = []
        for row in df.values:
            entry.append(
                DataWBReportDaily(
                    realizationreport_id=realizationreport_id,
                    gi_id=row[1],
                    subject_name=row[2],
                    sku=row[3],
                    brand=row[4],
                    vendor_code=row[5],
                    size=row[7],
                    barcode=row[8],
                    doc_type_name=row[9],
                    quantity=int(row[13]),
                    retail_price=round(float(row[14]), 2),
                    retail_amount=round(float(row[15]), 2),
                    sale_percent=int(row[18]),
                    commission_percent=round(float(row[23]), 2),
                    office_name=row[49],
                    supplier_oper_name=row[10],
                    order_date=datetime.datetime.strptime(row[11], "%Y-%m-%d").date(),
                    sale_date=datetime.datetime.strptime(row[12], "%Y-%m-%d").date(),
                    operation_date=date,
                    shk_id=row[55],
                    retail_price_withdisc_rub=round(float(row[19]), 2),
                    delivery_amount=int(row[34]),
                    return_amount=int(row[35]),
                    delivery_rub=round(float(row[36]), 2),
                    gi_box_type_name=row[51],
                    product_discount_for_report=round(float(row[16]), 2),
                    supplier_promo=round(float(row[17]), 2) if row[17] else 0,
                    order_id="0",
                    ppvz_spp_prc=round(float(row[22]), 2),
                    ppvz_kvw_prc_base=round(float(row[24]), 2),
                    ppvz_kvw_prc=round(float(row[25]), 2),
                    sup_rating_prc_up=round(float(row[20]), 2),
                    is_kgvp_v2=round(float(row[21]), 2),
                    ppvz_sales_commission=round(float(row[26]), 2),
                    ppvz_for_pay=round(float(row[33]), 2),
                    ppvz_reward=round(float(row[27]), 2),
                    acquiring_fee=round(float(row[28]), 2),
                    acquiring_bank=row[44],
                    ppvz_vw=round(float(row[31]), 2),
                    ppvz_vw_nds=round(float(row[32]), 2),
                    ppvz_office_id=row[45] or "0",
                    ppvz_office_name=row[46],
                    ppvz_supplier_id="0",
                    ppvz_supplier_name=row[48],
                    ppvz_inn=row[47],
                    declaration_number=row[52],
                    bonus_type_name=row[42] or None,
                    sticker_id=row[43] or "0",
                    site_country=row[50],
                    penalty=round(float(row[40]), 2),
                    additional_payment=round(float(row[41]), 2),
                    rebill_logistic_cost=round(float(row[57]), 2),
                    rebill_logistic_org=row[58] or None,
                    kiz=row[54] or None,
                    storage_fee=round(float(row[59]), 2),
                    deduction=round(float(row[60]), 2),
                    acceptance=round(float(row[61]), 2),
                    posting_number=row[56],
                )
            )
        return entry

    def alert_filter(self, row: DataWBReportDaily):
        return row.supplier_oper_name in self.alerts.keys() and (row.deduction or row.penalty)

    def post_alerts(self, list_report: list[DataWBReportDaily]):
        try:
            disable_notification = True
            text = f"*{self.market.entrepreneur}*\n\n"

            filtered_list_report = list(filter(self.alert_filter, list_report))
            if not filtered_list_report:
                return

            for row in filtered_list_report:
                self.alerts.setdefault(row.supplier_oper_name, {})
                self.alerts[row.supplier_oper_name].setdefault(row.bonus_type_name, 0)
                self.alerts[row.supplier_oper_name][row.bonus_type_name] += row.deduction or row.penalty

            for alert in sorted(self.alerts.keys()):
                alert_types = self.alerts.get(alert)
                if not alert_types:
                    continue

                text += f"*{alert}:*\n"
                for alert_type in sorted(alert_types.keys()):
                    cost = alert_types.get(alert_type, 0)
                    if cost:
                        if cost >= 1000:
                            text += "‼️ "
                            disable_notification = False
                        text += f"{alert_type}: *{cost}*\n"
                text += "\n"

            request_telegram(text, disable_notification)
        except Exception as e:
            logger.error(f"При отправки сообщения в Telegram произошла непредвиденная ошибка: {str(e)}")

    def save_data_in_database(self, date: datetime.date):
        all_entry = []

        if not self.new_path or not os.path.exists(self.new_path):
            logger.error("save_data_in_database: self.new_path не задан или папка не существует")
            return

        for zip_file in filter(lambda x: x.endswith(".zip"), os.listdir(self.new_path)):
            zip_file_path = os.path.join(self.new_path, zip_file)

            try:
                with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
                    file = zip_ref.namelist()[0]
                    zip_ref.extract(file, self.new_path)

                excel_file_path = os.path.join(self.new_path, file)

                with pd.ExcelFile(excel_file_path) as excel_file:
                    realizationreport_id = zip_file.split(".")[0].split("№")[-1]
                    if "_" in realizationreport_id:
                        realizationreport_id = realizationreport_id.split("_")[0]

                    entry = self.excel_to_entry(
                        excel_file=excel_file,
                        realizationreport_id=realizationreport_id,
                        date=date,
                    )

                os.remove(excel_file_path)

                all_entry.extend(entry)

                self.db_conn_arris.add_wb_report_daily_entry(
                    client_id=self.client_id,
                    list_report=entry,
                    date=date,
                    realizationreport_id=realizationreport_id,
                )

            except Exception as e:
                logger.error(f"save_data_in_database: ошибка обработки {zip_file}: {e}")

        self.post_alerts(list_report=all_entry)


# Чтобы твой main.py НЕ МЕНЯТЬ:
# driver = WebDriver(...) будет работать, но реально это BrowserController.


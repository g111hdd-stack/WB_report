from log_api.log import logger
from web_driver.wd import  BrowserController
from database.db import DbConnection
from config import DB_ADMIN_URL, DB_ARRIS_URL
from sqlalchemy import text

def main():
    db_conn_admin = DbConnection(url=DB_ADMIN_URL)
    db_conn_arris = DbConnection(url=DB_ARRIS_URL)
    try:
        print("Проверка подключения ADMIN:",
              db_conn_admin.session.execute(text("SELECT 1")).scalar())
        print("Проверка подключения ARRIS:",
              db_conn_arris.session.execute(text("SELECT 1")).scalar())
    except Exception as e:
        print("Ошибка подключения к БД:", e)
        return
    try:
        markets = db_conn_admin.get_markets()

        for market in markets:
            driver =  BrowserController(market=market,
                               user='WBReportBot',
                               db_conn_admin=db_conn_admin,
                               db_conn_arris=db_conn_arris)
            driver.load_url(url=market.marketplace_info.link)
            if driver.is_browser_active():
                driver.stores_report_daily()
                driver.quit()
                logger.info(f"Сбор отчётов компани {market.name_company} завершен")
            else:
                logger.error(f"Сбор отчётов компани {market.name_company} прерван")

        else:
            logger.info(f"Сбор отчётов завершен")
    except Exception as e:
        logger.error(e)
    finally:
        db_conn_admin.session.close()
        db_conn_arris.session.close()


if __name__ == '__main__':
    main()

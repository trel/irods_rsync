from redis import StrictRedis
from rq import Queue, Worker
from rq.worker import WorkerStatus
from os import listdir
from os.path import isfile, join, getmtime, realpath, relpath
from datetime import datetime
from rq_scheduler import Scheduler
import logging
import sys
import redis_lock
import sync_irods

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s:%(name)s:%(levelname)s:%(message)s")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)
logger.addHandler(handler)

def sync_time_key(path):
    return "sync_time:/"+path

def get_with_key(r, key, path):
    sync_time_bs = r.get(key(path))
    if sync_time_bs == None:
        sync_time = None
    else:
        sync_time = float(sync_time_bs)
    return sync_time

def set_with_key(r, path, key, sync_time):
    r.set(key(path), sync_time)

def sync_path(path_q_name, file_q_name, target, root, path):
    try:
        r = StrictRedis()
        if isfile(path):
            logger.info("enqueue file path " + path)
            q = Queue(path_q_name, connection=r)
            q.enqueue(sync_file, target, root, path)
        else:
            logger.info("walk dir " + path)
            q = Queue(file_q_name, connection=r)
            for n in listdir(path):
                q.enqueue(sync_path, path_q_name, file_q_name, target, root, join(path, n))
    except OSError as err:
        logger.warning("Warning: " + str(err))        
    except Exception as err:
        logger.error("Unexpected error: " + str(err))
        raise

def sync_file(target, root, path):
    try:
        r = StrictRedis()
        with redis_lock.Lock(r, path):
            t = datetime.now().timestamp()
            sync_time = get_with_key(r, sync_time_key, path)
            mtime = getmtime(path)
            if sync_time == None or mtime > sync_time:
                logger.info("synchronizing file. path = " + path + ", t0 = " + str(sync_time) + ", t = " + str(t) + ", mtime = " + str(mtime) + ".")
                sync_irods.sync_data_from_file(join(target, relpath(path, start=root)), path)
                set_with_key(r, path, sync_time_key, str(t))
            else:
                logger.info("file hasn't changed. path = " + path + ".")
    except OSError as err:
        logger.warning("Warning: " + str(err))        
    except Exception as err:
        logger.error("Unexpected error: " + str(err))
        raise


RESTART_JOB_ID = "restart"

def restart(restart_q_name, path_q_name, file_q_name, target, root, path, interval):
    try:
        logger.info("***************** restart *****************")
        r = StrictRedis()
        path_q = Queue(path_q_name, connection=r)
        file_q = Queue(file_q_name, connection=r)
        restart_q = Queue(restart_q_name, connection=r)
        path_q_workers = Worker.all(queue=path_q)
        file_q_workers = Worker.all(queue=file_q)

        def all_not_busy(ws):
            return all(w.get_state() != WorkerStatus.BUSY or w.get_current_job_id() == RESTART_JOB_ID for w in ws)            

        if all_not_busy(path_q_workers) and all_not_busy(file_q_workers) and path_q.is_empty() and file_q.is_empty():
            path_q.enqueue(sync_path, path_q_name, file_q_name, target, root, path)
        else:
            logger.info("queue not empty or worker busy" + str(path_q.get_job_ids()))

        scheduler = Scheduler(queue=restart_q, connection=r)
        scheduler.enqueue_in(interval, restart, restart_q_name, path_q_name, file_q_name, target, root, path, interval, job_id=RESTART_JOB_ID)
    except OSError as err:
        logger.warning("Warning: " + str(err))        
    except Exception as err:
        logger.error("Unexpected error: " + str(err))
        raise

def start_synchronization(restart_q_name, path_q_name, file_q_name, target, root, interval):
    root_abs = realpath(root)

    q = Queue("restart", connection=StrictRedis())
    q.enqueue(restart, restart_q_name, path_q_name, file_q_name, target, root_abs, root_abs, interval, job_id=RESTART_JOB_ID)
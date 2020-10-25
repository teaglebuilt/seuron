from igneous_and_cloudvolume import submit_igneous_tasks
from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.utils.weight_rule import WeightRule
from datetime import datetime
from slack_message import task_failure_alert

igneous_default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2019, 2, 8),
    'catchup': False,
    'retries': 0,
}

dag_igneous = DAG("igneous", default_args=igneous_default_args, schedule_interval=None)

submit_tasks = PythonOperator(
    task_id="submit_igneous_tasks",
    python_callable=submit_igneous_tasks,
    priority_weight=100000,
    on_failure_callback=task_failure_alert,
    weight_rule=WeightRule.ABSOLUTE,
    queue="manager",
    dag=dag_igneous
)



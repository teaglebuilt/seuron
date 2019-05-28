from airflow.operators.docker_plugin import DockerWithVariablesOperator
from airflow.utils.weight_rule import WeightRule
from datetime import timedelta
from param_default import default_args, cv_path, cmd_proto
from slack_message import task_retry_alert


def composite_chunks_wrap_op(img, dag, config_mounts, queue, tag, stage, op):
    cmdlist = "export STAGE={} && /root/seg/scripts/run_wrapper.sh . composite_chunk_{} {}".format(stage, op, tag)

    return DockerWithVariablesOperator(
        config_mounts,
        mount_point=cv_path,
        task_id='composite_chunk_{}_{}'.format(stage, tag),
        command=cmd_proto.format(cmdlist),
        default_args=default_args,
        image=img,
        on_retry_callback=task_retry_alert,
        weight_rule=WeightRule.ABSOLUTE,
        execution_timeout=timedelta(minutes=11520),
        queue=queue,
        dag=dag
    )


def composite_chunks_batch_op(img, dag, config_mounts, queue, mip, tag, stage, op):
    cmdlist = "export STAGE={} && /root/seg/scripts/run_batch.sh {} {} {}".format(stage, op, mip, tag)

    return DockerWithVariablesOperator(
        config_mounts,
        mount_point=cv_path,
        task_id='composite_chunk_{}_{}'.format(stage, tag),
        command=cmd_proto.format(cmdlist),
        default_args=default_args,
        image=img,
        on_retry_callback=task_retry_alert,
        weight_rule=WeightRule.ABSOLUTE,
        execution_timeout=timedelta(minutes=180),
        queue=queue,
        dag=dag
    )


def remap_chunks_batch_op(img, dag, config_mounts, queue, mip, tag, stage, op):
    cmdlist = "export STAGE={} && /root/seg/scripts/remap_batch.sh {} {} {}".format(stage, stage, mip, tag)
    return DockerWithVariablesOperator(
        config_mounts,
        mount_point=cv_path,
        task_id='remap_chunk_{}_{}'.format(stage, tag),
        command=cmd_proto.format(cmdlist),
        default_args=default_args,
        image=img,
        on_retry_callback=task_retry_alert,
        weight_rule=WeightRule.ABSOLUTE,
        execution_timeout=timedelta(minutes=180),
        queue=queue,
        dag=dag
    )

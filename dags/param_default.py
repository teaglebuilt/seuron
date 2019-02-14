param_default = {
    "NAME":"minnie_367_0",

    "SCRATCH_PREFIX":"gs://ranl-scratch/",

    "AFF_PATH":"gs://microns-seunglab/minnie_v0/minnie10/affinitymap/test",
    "AFF_MIP":"1",

    "WS_PREFIX":"gs://microns-seunglab/minnie_v0/minnie10/",

    "SEG_PREFIX":"gs://microns-seunglab/minnie_v0/minnie10/",

    "WS_HIGH_THRESHOLD":"0.99",
    "WS_LOW_THRESHOLD":"0.01",
    "WS_SIZE_THRESHOLD":"200",

    "AGG_THRESHOLD":"0.25",
    "WS_IMAGE":"ranlu/watershed:ranl_minnie_exp",
    "AGG_IMAGE":"ranlu/agglomeration:ranl_minnie_exp",
    "BBOX": [126280+256, 64280+256, 20826-200, 148720-256, 148720-256, 20993],
    "RESOLUTION": [8,8,40],
    "CHUNK_SIZE": [512, 512, 128]
}

cv_chunk_size=[128,128,16]
batch_mip = 2
high_mip = 5
import numpy as np
import tensorflow as tf
import keras
import keras.layers as L
from kaggle_datasets import KaggleDatasets


def connect_to_tpu(tpu_address: str = None):
    if tpu_address is not None:  # When using GCP
        cluster_resolver = tf.distribute.cluster_resolver.TPUClusterResolver(
            tpu=tpu_address)
        if tpu_address not in ("", "local"):
            tf.config.experimental_connect_to_cluster(cluster_resolver)
        tf.tpu.experimental.initialize_tpu_system(cluster_resolver)
        strategy = tf.distribute.experimental.TPUStrategy(cluster_resolver)
        print("Running on TPU ", cluster_resolver.master())
        print("REPLICAS: ", strategy.num_replicas_in_sync)
        return cluster_resolver, strategy
    else:  # When using Colab or Kaggle
        try:
            cluster_resolver = tf.distribute.cluster_resolver.TPUClusterResolver.connect()
            strategy = tf.distribute.experimental.TPUStrategy(cluster_resolver)
            print("Running on TPU ", cluster_resolver.master())
            print("REPLICAS: ", strategy.num_replicas_in_sync)
            return cluster_resolver, strategy
        except:
            print("WARNING: No TPU detected.")
            mirrored_strategy = tf.distribute.MirroredStrategy()
            return None, mirrored_strategy


AUTO = tf.data.experimental.AUTOTUNE
GCS_DS_Path = KaggleDatasets().get_gcs_path('tpu-getting-started')
print(GCS_DS_Path)
IMAGE_SIZE = [224, 224]
GCS_PATH = GCS_DS_Path + '/tfrecords-jpeg-224x224'
training_file = tf.io.gfile.glob(GCS_PATH + '/train/*.tfrec')
test_file = tf.io.gfile.glob(GCS_PATH + '/test/*.tfrec')
valid_file = tf.io.gfile.glob(GCS_PATH + '/val/*.tfrec')


def decode_image(image_data):
    image = tf.image.decode_jpeg(image_data, channels=3)
    image = tf.cast(image, tf.float32) / 255.0
    image = tf.reshape(image, [*IMAGE_SIZE, 3])
    return image


def read_labeled_tfrecord(example):
    LABELED_TFREC_FORMAT = {
        "image": tf.io.FixedLenFeature([], tf.string),  # tf.string means bytestring
        "class": tf.io.FixedLenFeature([], tf.int64),  # shape [] means single element
    }
    example = tf.io.parse_single_example(example, LABELED_TFREC_FORMAT)
    image = decode_image(example['image'])
    label = tf.cast(example['class'], tf.int32)
    return image, label  # returns a dataset of (image, label) pairs


def read_unlabeled_tfrecord(example):
    UNLABELED_TFREC_FORMAT = {
        "image": tf.io.FixedLenFeature([], tf.string),  # tf.string means bytestring
        "id": tf.io.FixedLenFeature([], tf.string),  # shape [] means single element
        # class is missing, this competitions's challenge is to predict flower classes for the test dataset
    }
    example = tf.io.parse_single_example(example, UNLABELED_TFREC_FORMAT)
    image = decode_image(example['image'])
    idnum = example['id']
    return image, idnum  # returns a dataset of image(s)


def load_dataset(filenames, labeled=True, ordered=False):
    # Read from TFRecords. For optimal performance, reading from multiple files at once and
    # disregarding data order. Order does not matter since we will be shuffling the data anyway.

    ignore_order = tf.data.Options()
    if not ordered:
        ignore_order.experimental_deterministic = False  # disable order, increase speed

    dataset = tf.data.TFRecordDataset(filenames,
                                      num_parallel_reads=AUTO)  # automatically interleaves reads from multiple files
    dataset = dataset.with_options(
        ignore_order)  # uses data as soon as it streams in, rather than in its original order
    dataset = dataset.map(read_labeled_tfrecord if labeled else read_unlabeled_tfrecord, num_parallel_calls=AUTO)
    # returns a dataset of (image, label) pairs if labeled=True or (image, id) pairs if labeled=False
    return dataset


def get_training_dataset():
    dataset = load_dataset(training_file, labeled=True)
    dataset = dataset.repeat()  # the training dataset must repeat for several epochs
    dataset = dataset.shuffle(2048)
    dataset = dataset.batch(BATCH_SIZE)
    return dataset


def get_validation_dataset(ordered=False):
    dataset = load_dataset(valid_file, labeled=True, ordered=ordered)
    dataset = dataset.batch(BATCH_SIZE)
    dataset = dataset.cache()
    return dataset


def get_test_dataset(ordered=False):
    dataset = load_dataset(test_file, labeled=False, ordered=ordered)
    dataset = dataset.batch(BATCH_SIZE)
    return dataset


strategy = connect_to_tpu('jg-tpu')

BATCH_SIZE = 16 * strategy.num_replicas_in_sync

ds_train = get_training_dataset()
ds_valid = get_validation_dataset()
ds_test = get_test_dataset()
ds_iter = iter(ds_train.unbatch().batch(20))
one_batch = next(ds_iter)


def convblock(filter_size, is_block2=False):
    model.add(L.Conv2D(filter_size, kernel_size=(3, 3), padding='same', activation='relu'))
    model.add(L.Conv2D(filter_size, kernel_size=(3, 3), padding='same', activation='relu'))
    if is_block2:
        model.add(L.Conv2D(filter_size, kernel_size=(3, 3), padding='same', activation='relu'))
    model.add(L.MaxPool2D(pool_size=(2, 2), strides=(2, 2), padding='same'))


weights = keras.utils.get_file('vgg16_weights',
                               'https://storage.googleapis.com/tensorflow/keras-applications/vgg16/vgg16_weights_tf_dim_ordering_tf_kernels.h5')

with strategy.scope():
    model = keras.Sequential()
    model.add(L.InputLayer(input_shape=(224, 224, 3)))
    convblock(64)

    convblock(128)

    convblock(256, is_block2=True)

    convblock(512, is_block2=True)

    convblock(512, is_block2=True)
    model.add(L.Flatten())
    model.add(L.Dense(4096, activation='relu'))
    model.add(L.Dense(4096, activation='relu'))
    model.add(L.Dense(1000, activation='relu'))
    model.load_weights(weights)
    for Layers in model.layers:
        Layers.trainable = False
    model.add(L.Dense(104, activation='softmax'))  # since our dataset have 104 classes

model.compile(
    optimizer=keras.optimizers.Adam(learning_rate=0.001),
    loss='sparse_categorical_crossentropy',
    metrics=['sparse_categorical_accuracy'],
)

NUM_TRAINING_IMAGES = 12753
NUM_TEST_IMAGES = 7382
STEPS_PER_EPOCH = NUM_TRAINING_IMAGES // BATCH_SIZE

history = model.fit(
    ds_train,
    validation_data=ds_valid,
    epochs=50, steps_per_epoch=STEPS_PER_EPOCH
)

test_ds = get_test_dataset(ordered=True)

print('Computing predictions...')
test_images_ds = test_ds.map(lambda image, idnum: image)
probabilities = model.predict(test_images_ds)
predictions = np.argmax(probabilities, axis=-1)
print(predictions)

print('Generating submission.csv file...')

# Get image ids from test set and convert to unicode
test_ids_ds = test_ds.map(lambda image, idnum: idnum).unbatch()
test_ids = next(iter(test_ids_ds.batch(NUM_TEST_IMAGES))).numpy().astype('U')

np.savetxt(
    'submission.csv',
    np.rec.fromarrays([test_ids, predictions]),
    fmt=['%s', '%d'],
    delimiter=',',
    header='id,label',
    comments='',
)
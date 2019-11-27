TF_INC=$(python3 -c 'import tensorflow as tf; print(tf.sysconfig.get_include())')

/usr/local/cuda-10.0/bin/nvcc -std=c++11 -c -DNDEBUG -o sequential_batch_fft_kernel.cu.o \
  sequential_batch_fft_kernel.cu.cc \
  -I $TF_INC -D GOOGLE_CUDA=1 -x cu -Xcompiler -fPIC
mkdir build
g++ -std=c++11 -shared -DNDEBUG -o ./build/sequential_batch_fft.so \
  sequential_batch_fft_kernel.cu.o \
  sequential_batch_fft.cc \
  -I $TF_INC -fPIC  -L /usr/local/cuda-10.0/lib64/ \
  -D_GLIBCXX_USE_CXX11_ABI=0 \
  -lcudart -lcufft -L/usr/local/lib/python3.6/dist-packages/tensorflow_core/ -l:libtensorflow_framework.so.1 

rm -rf sequential_batch_fft_kernel.cu.o

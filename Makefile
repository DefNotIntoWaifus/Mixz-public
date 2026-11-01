CC ?= gcc
CFLAGS ?= -O3 -march=native -fPIC -fvisibility=hidden -Wall -Wextra -Wno-unused-parameter

ZLIB := engine/zlib-1.3.1
SRCS := engine/cm.c engine/transform.c engine/pdeflate.c $(addprefix $(ZLIB)/,deflate.c trees.c zutil.c adler32.c crc32.c)

libmixz.so: $(SRCS) $(wildcard $(ZLIB)/*.h)
	$(CC) $(CFLAGS) -DZ_PREFIX -I$(ZLIB) -shared -o $@ $(SRCS)

clean:
	rm -f libmixz.so

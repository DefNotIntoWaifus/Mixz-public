#include <stdio.h>
#include <stdlib.h>
#include <time.h>
long long mz_compress(const unsigned char*, size_t, const unsigned char*, size_t, int,int,int,int, unsigned char*, size_t);
int main(int argc,char**argv){FILE*f=fopen(argv[1],"rb");fseek(f,0,2);long n=ftell(f);fseek(f,0,0);unsigned char*d=malloc(n);if(fread(d,1,n,f)!=(size_t)n)return 1;
int mb=argc>3?atoi(argv[3]):20; unsigned char*o=malloc(n*2+1024);struct timespec t0,t1;clock_gettime(CLOCK_MONOTONIC,&t0);
long long r=mz_compress(d,n,0,0,atoi(argv[2]),0,1,mb,o,n*2+1024);clock_gettime(CLOCK_MONOTONIC,&t1);
double s=(t1.tv_sec-t0.tv_sec)+(t1.tv_nsec-t0.tv_nsec)/1e9;
printf("maxbits %d: %ld -> %lld (%.2f%%)  %.3f MB/s  %.0f ns/byte\n",mb,n,r,100.0*r/n,n/1e6/s,s*1e9/n);}

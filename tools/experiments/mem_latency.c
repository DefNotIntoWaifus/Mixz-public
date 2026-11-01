#include <stdio.h>
#include <stdlib.h>
#include <time.h>
int main(){ size_t sizes[]={32<<10,256<<10,2<<20,8<<20,32<<20,128<<20};
for(int s=0;s<6;s++){ size_t n=sizes[s]/sizeof(size_t); size_t*a=malloc(n*sizeof(size_t)); size_t*p=malloc(n*sizeof(size_t));
 for(size_t i=0;i<n;i++)p[i]=i; srand(1); for(size_t i=n-1;i>0;i--){size_t j=((size_t)rand()*RAND_MAX+rand())%(i+1);size_t t=p[i];p[i]=p[j];p[j]=t;}
 for(size_t i=0;i<n;i++)a[p[i]]=p[(i+1)%n];
 size_t x=p[0]; long iters=20000000; struct timespec t0,t1; clock_gettime(CLOCK_MONOTONIC,&t0);
 for(long i=0;i<iters;i++)x=a[x]; clock_gettime(CLOCK_MONOTONIC,&t1);
 double ns=((t1.tv_sec-t0.tv_sec)*1e9+(t1.tv_nsec-t0.tv_nsec))/iters;
 printf("table %6zu KB: %6.1f ns per dependent random read (x=%zu)\n",sizes[s]>>10,ns,x&1); free(a);free(p);} }

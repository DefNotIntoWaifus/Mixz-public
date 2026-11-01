import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
HERE = os.path.dirname(os.path.abspath(__file__))
import ctypes, subprocess
SRC=os.path.join(ROOT, 'engine', 'mixz.c')
def build(defs, name):
    out=os.path.join(HERE, f'lib_{name}.so')
    subprocess.check_call(['gcc','-O2','-fPIC','-shared','-o',out,SRC]+[f'-D{k}={v}' for k,v in defs.items()])
    lib=ctypes.CDLL(out)
    lib.mz_compress.restype=ctypes.c_longlong
    lib.mz_compress.argtypes=[ctypes.c_char_p,ctypes.c_size_t,ctypes.c_char_p,ctypes.c_size_t,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_char_p,ctypes.c_size_t]
    return lib
def size(lib,d,prof):
    cap=len(d)*2+1024; out=ctypes.create_string_buffer(cap)
    return lib.mz_compress(d,len(d),b'',0,prof,0,1,20,out,cap)
t=open(sys.argv[1],'rb').read(); b=open(sys.argv[2],'rb').read()
for spec in sys.argv[3:]:
    defs=dict(kv.split('=') for kv in spec.split(',')) if spec!='base' else {}
    lib=build(defs, spec.replace('=','').replace(',','_').replace('(','').replace(')','').replace('<','').replace(' ',''))
    st=size(lib,t,0); sb=size(lib,b,1)
    print(f"{spec:40s} text {st} bin {sb} sum {st+sb}", flush=True)

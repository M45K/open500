#!/usr/bin/env python3
# Emulatore SH minimale (subset intero) per MPC500 RE.
# Memoria: flash mpc500.bin mappato a 0xA0000000 (offset 0), + regioni RAM/MMIO.
import struct
class SH:
    def __init__(self):
        self.r=[0]*16; self.pc=0; self.pr=0; self.macl=0; self.mach=0
        self.t=0; self.gbr=0; self.vbr=0; self.q=0; self.mflag=0; self.timer=0
        self.ram={}          # dict di regioni: (base,end)->bytearray
        self.mmio_r={}       # addr->func(addr) letture MMIO
        self.mmio_w={}       # addr->func(addr,val)
        self.flash=None; self.flash_base=0
        self.trace=False; self.steps=0; self.maxsteps=2_000_000
        self.wlog=[]         # log scritture (addr,val,size) per osservare copie
        self.logrange=None   # (lo,hi) per loggare scritture
    def map_flash(self,data,base=0xA0000000): self.flash=data; self.flash_base=base
    def map_ram(self,base,size): self.ram[(base,base+size)]=bytearray(size)
    def _region(self,addr):
        addr&=0xffffffff
        # SDRAM (area 2) vista da P2 non cacheata 0xA8.. = stessa memoria fisica di P1 0x88.. (come sull'hardware)
        cands=(addr-0x20000000,addr) if 0xA8000000<=addr<0xB0000000 else (addr,)
        for a in cands:
            for (b,e),buf in self.ram.items():
                if b<=a<e: return buf,a-b
        return None,None
    def rd(self,addr,size):
        addr&=0xffffffff
        if addr in self.mmio_r: return self.mmio_r[addr](addr)&((1<<(size*8))-1)
        if 0xFFFFFE90<=addr<=0xFFFFFEC0:   # TMU timer on-chip -> free-running
            self.timer=(self.timer+0x40)&0xffffffff; return self.timer&((1<<(size*8))-1)
        buf,off=self._region(addr)
        if buf is not None:
            v=int.from_bytes(buf[off:off+size],'little'); return v
        if self.flash is not None and self.flash_base<=addr<self.flash_base+len(self.flash):
            o=addr-self.flash_base; return int.from_bytes(self.flash[o:o+size],'little')
        return 0
    def wr(self,addr,val,size):
        addr&=0xffffffff; val&=(1<<(size*8))-1
        if self.logrange and self.logrange[0]<=addr<self.logrange[1]:
            self.wlog.append((self.pc,addr,val,size))
        if addr in self.mmio_w: self.mmio_w[addr](addr,val); return
        buf,off=self._region(addr)
        if buf is not None: buf[off:off+size]=val.to_bytes(size,'little'); return
        # scrittura su indirizzo non mappato (MMIO/SDRAM): auto-map come RAM a pagine 64KB
        if 0xA0000000<=addr<0xA0000000+len(self.flash): return  # non scrivere sul flash
        page=addr&0xFFFF0000
        self.map_ram(page,0x10000); buf,off=self._region(addr)
        if buf is not None: buf[off:off+size]=val.to_bytes(size,'little')
    def s8(self,v): return v-0x100 if v&0x80 else v
    def s12(self,v): return v-0x1000 if v&0x800 else v
    def s32(self,v): v&=0xffffffff; return v-0x100000000 if v&0x80000000 else v
    def step(self):
        pc=self.pc; w=self.rd(pc,2); self.pc=(pc+2)&0xffffffff
        n=(w>>8)&0xf; m=(w>>4)&0xf; d=w&0xff; d4=w&0xf; hi=w>>12
        r=self.r
        def S(i,v): r[i]=v&0xffffffff
        def s8(v): return v-0x100 if v&0x80 else v
        def s12(v): return v-0x1000 if v&0x800 else v
        def s16(v): return v-0x10000 if v&0x8000 else v
        def s32(v): v&=0xffffffff; return v-0x100000000 if v&0x80000000 else v
        # deleghe delay-slot
        def dslot(target):
            self.step(); self.pc=target&0xffffffff
        if hi==0xE: S(n, s8(d)); return                               # mov #imm,rn
        if hi==0x9: a=(pc+4+d*2)&0xffffffff; S(n, s16(self.rd(a,2))); return  # mov.w @(disp,pc),rn
        if hi==0xD: a=((pc&~3)+4+d*4)&0xffffffff; S(n, self.rd(a,4)); return  # mov.l @(disp,pc),rn
        if hi==0x7: S(n, r[n]+s8(d)); return                          # add #imm,rn
        if hi==0x6:
            if d4==0x0: S(n, s8(self.rd(r[m],1))); return             # mov.b @rm,rn
            if d4==0x1: S(n, s16(self.rd(r[m],2))); return            # mov.w @rm,rn
            if d4==0x2: S(n, self.rd(r[m],4)); return                 # mov.l @rm,rn
            if d4==0x3: S(n, r[m]); return                            # mov rm,rn
            if d4==0x4: S(n, s8(self.rd(r[m],1))); S(m, r[m]+1); return  # mov.b @rm+,rn
            if d4==0x5: S(n, s16(self.rd(r[m],2))); S(m, r[m]+2); return # mov.w @rm+,rn
            if d4==0x6: S(n, self.rd(r[m],4)); S(m, r[m]+4); return    # mov.l @rm+,rn
            if d4==0x7: S(n, ~r[m]); return                           # not
            if d4==0x8: v=r[m]; S(n, ((v&0xff)<<8)|((v>>8)&0xff)|(v&0xffff0000)); return  # swap.b
            if d4==0x9: v=r[m]; S(n, ((v&0xffff)<<16)|((v>>16)&0xffff)); return           # swap.w
            if d4==0xa: t=r[m]+self.t; S(n, -(t)&0xffffffff) if False else S(n,(0-r[m]-self.t)); self.t=1 if (0-r[m]-self.t)<0 else 0; return  # negc(approx)
            if d4==0xb: S(n, (0-r[m])); return                        # neg
            if d4==0xc: S(n, r[m]&0xff); return                       # extu.b
            if d4==0xd: S(n, r[m]&0xffff); return                     # extu.w
            if d4==0xe: S(n, s8(r[m]&0xff)); return                   # exts.b
            if d4==0xf: S(n, s16(r[m]&0xffff)); return                # exts.w
        if hi==0x2:
            if d4==0x0: self.wr(r[n], r[m], 1); return                # mov.b rm,@rn
            if d4==0x1: self.wr(r[n], r[m], 2); return                # mov.w rm,@rn
            if d4==0x2: self.wr(r[n], r[m], 4); return                # mov.l rm,@rn
            if d4==0x4: S(n, r[n]-1); self.wr(r[n], r[m], 1); return  # mov.b rm,@-rn
            if d4==0x5: S(n, r[n]-2); self.wr(r[n], r[m], 2); return  # mov.w rm,@-rn
            if d4==0x6: S(n, r[n]-4); self.wr(r[n], r[m], 4); return  # mov.l rm,@-rn
            if d4==0x7: self.q=(r[n]>>31)&1; self.mflag=(r[m]>>31)&1; self.t=self.q^self.mflag; return  # div0s
            if d4==0x8: self.t=1 if (r[n]&r[m])==0 else 0; return     # tst
            if d4==0x9: S(n, r[n]&r[m]); return                       # and
            if d4==0xa: S(n, r[n]^r[m]); return                       # xor
            if d4==0xb: S(n, r[n]|r[m]); return                       # or
            if d4==0xd: S(n, ((r[m]<<16)|((r[n]&0xffffffff)>>16))); return  # xtrct
            if d4==0xc: bn=[(r[n]>>(8*i))&0xff for i in range(4)]; bm=[(r[m]>>(8*i))&0xff for i in range(4)]; self.t=1 if any(bn[i]==bm[i] for i in range(4)) else 0; return  # cmp/str
            if d4==0xe: self.macl=((r[n]&0xffff)*(r[m]&0xffff))&0xffffffff; return  # mulu.w
            if d4==0xf: self.macl=(s16(r[n]&0xffff)*s16(r[m]&0xffff))&0xffffffff; return  # muls.w
        if hi==0x3:
            if d4==0x4:  # div1 Rm,Rn
                tmp2=r[m]&0xffffffff; old_q=self.q; self.q=(r[n]>>31)&1
                r[n]=((r[n]<<1)|self.t)&0xffffffff
                if old_q==0:
                    if self.mflag==0:
                        t0=r[n]; r[n]=(r[n]-tmp2)&0xffffffff; t1=1 if r[n]>t0 else 0
                        self.q=t1 if self.q==0 else (1 if t1==0 else 0)
                    else:
                        t0=r[n]; r[n]=(r[n]+tmp2)&0xffffffff; t1=1 if r[n]<t0 else 0
                        self.q=(1 if t1==0 else 0) if self.q==0 else t1
                else:
                    if self.mflag==0:
                        t0=r[n]; r[n]=(r[n]+tmp2)&0xffffffff; t1=1 if r[n]<t0 else 0
                        self.q=t1 if self.q==0 else (1 if t1==0 else 0)
                    else:
                        t0=r[n]; r[n]=(r[n]-tmp2)&0xffffffff; t1=1 if r[n]>t0 else 0
                        self.q=(1 if t1==0 else 0) if self.q==0 else t1
                self.t=1 if self.q==self.mflag else 0; return
            if d4==0x0: self.t=1 if r[n]==r[m] else 0; return         # cmp/eq
            if d4==0x2: self.t=1 if r[n]>=r[m] else 0; return         # cmp/hs (unsigned)
            if d4==0x3: self.t=1 if s32(r[n])>=s32(r[m]) else 0; return  # cmp/ge
            if d4==0x6: self.t=1 if r[n]>r[m] else 0; return          # cmp/hi
            if d4==0x7: self.t=1 if s32(r[n])>s32(r[m]) else 0; return  # cmp/gt
            if d4==0xc: S(n, r[n]+r[m]); return                       # add
            if d4==0x8: S(n, r[n]-r[m]); return                       # sub
            if d4==0xe: t=r[n]+r[m]+self.t; self.t=1 if t>0xffffffff else 0; S(n,t); return  # addc
            if d4==0xa: t=r[n]-r[m]-self.t; self.t=1 if t<0 else 0; S(n,t); return  # subc
            if d4==0x5: res=(r[n]&0xffffffff)*(r[m]&0xffffffff); self.macl=res&0xffffffff; self.mach=(res>>32)&0xffffffff; return  # dmulu.l
            if d4==0xd: res=s32(r[n])*s32(r[m]); self.macl=res&0xffffffff; self.mach=(res>>32)&0xffffffff; return  # dmuls.l
        if hi==0x1: self.wr(r[n]+d4*4, r[m], 4); return               # mov.l rm,@(disp,rn)
        if hi==0x5: S(n, self.rd(r[m]+d4*4, 4)); return               # mov.l @(disp,rm),rn
        if hi==0x8:
            if n==0x0: self.wr(r[m]+d4, r[0], 1); return              # mov.b r0,@(disp,rm)
            if n==0x1: self.wr(r[m]+d4*2, r[0], 2); return            # mov.w r0,@(disp,rm)
            if n==0x4: S(0, s8(self.rd(r[m]+d4,1))); return           # mov.b @(disp,rm),r0
            if n==0x5: S(0, s16(self.rd(r[m]+d4*2,2))); return        # mov.w @(disp,rm),r0
            if n==0x8: self.t=1 if (r[0]&0xffffffff)==(s8(d)&0xffffffff) else 0; return  # cmp/eq #imm,r0
            if n==0x9: 
                if self.t: self.pc=(pc+4+s8(d)*2)&0xffffffff
                return                                                # bt
            if n==0xb:
                if not self.t: self.pc=(pc+4+s8(d)*2)&0xffffffff
                return                                                # bf
            if n==0xd:
                if self.t: dslot((pc+4+s8(d)*2))
                return                                                # bt/s
            if n==0xf:
                if not self.t: dslot((pc+4+s8(d)*2))
                return                                                # bf/s
        if hi==0xA: dslot((pc+4+s12(w&0xfff)*2)); return              # bra
        if hi==0xB: self.pr=(pc+4)&0xffffffff; dslot((pc+4+s12(w&0xfff)*2)); return  # bsr
        if hi==0xC:
            if n==0x7: S(0, ((pc&~3)+4+d*4)); return                  # mova @(disp,pc),r0
            if n==0x8: self.t=1 if (r[0]&0xff & d)==0 else 0; return  # tst #imm,r0
            if n==0x9: S(0, r[0]&d); return                          # and #imm,r0
            if n==0xa: S(0, r[0]^d); return                          # xor #imm,r0
            if n==0xb: S(0, r[0]|d); return                          # or #imm,r0
            if n==0x0: self.wr(self.gbr+d, r[0],1); return
            if n==0x1: self.wr(self.gbr+d*2, r[0],2); return                   # mov.w r0,@(disp,gbr)
            if n==0x2: self.wr(self.gbr+d*4, r[0],4); return                   # mov.l r0,@(disp,gbr)
            if n==0x5: v=self.rd(self.gbr+d*2,2); S(0, v-0x10000 if v&0x8000 else v); return   # mov.w @(disp,gbr),r0
            if n==0x6: S(0, self.rd(self.gbr+d*4,4)); return                   # mov.l @(disp,gbr),r0
            if n==0x4: S(0, s8(self.rd(self.gbr+d,1))); return
        if hi==0x0:
            if w==0x0008: self.t=0; return                           # clrt
            if w==0x0018: self.t=1; return                           # sett
            if w==0x0009: return                                     # nop
            if w==0x000b: t=self.pr; dslot(t); return                # rts
            if w==0x002b: self.srv=0; dslot(getattr(self,'spc',0)); return   # rte (SR <- SSR: banco 0)
            if w==0x0028: self.mach=0; self.macl=0; return           # clrmac
            if w==0x0019: self.mflag=0; self.q=0; self.t=0; return               # div0u
            if w==0x001b: return                                     # sleep
            if d4==0x3 and m==0x0: self.pr=(pc+4)&0xffffffff; dslot((pc+4+r[n])); return  # bsrf rn
            if d4==0x3 and m==0x2: dslot((pc+4+r[n])); return         # braf rn
            if d4==0xc: S(n, s8(self.rd(r[0]+r[m],1))); return        # mov.b @(r0,rm),rn
            if d4==0xd: S(n, s16(self.rd(r[0]+r[m],2))); return       # mov.w @(r0,rm),rn
            if d4==0xe: S(n, self.rd(r[0]+r[m],4)); return            # mov.l @(r0,rm),rn
            if d4==0x4: self.wr(r[0]+r[n], r[m],1); return            # mov.b rm,@(r0,rn)
            if d4==0x5: self.wr(r[0]+r[n], r[m],2); return            # mov.w rm,@(r0,rn)
            if d4==0x6: self.wr(r[0]+r[n], r[m],4); return            # mov.l rm,@(r0,rn)
            if d4==0x7: self.macl=(r[n]*r[m])&0xffffffff; return      # mul.l
            if d4==0xa and m==0x2: S(n, self.pr); return             # sts pr,rn
            if d4==0xa and m==0x0: S(n, self.mach); return           # sts mach,rn
            if d4==0xa and m==0x1: S(n, self.macl); return           # sts macl,rn
            if d4==0x2 and m==0x0: S(n, getattr(self,'srv',0)); return   # stc sr,rn (valore di test: self.srv, default 0)
            if d4==0x2 and m==0x1: S(n, self.gbr); return            # stc gbr,rn
            if d4==0x9 and m==0x2: S(n, self.t); return              # movt rn
        if hi==0x4:
            if d4==0xc:   # SHAD Rm,Rn (aritmetico)
                sh=s32(r[m])
                if sh>=0: S(n, r[n]<<(sh&0x1f))
                elif (sh&0x1f)==0: S(n, 0xffffffff if (r[n]>>31)&1 else 0)
                else: S(n, s32(r[n])>>((~sh&0x1f)+1))
                return
            if d4==0xd:   # SHLD Rm,Rn (logico)
                sh=s32(r[m])
                if sh>=0: S(n, r[n]<<(sh&0x1f))
                elif (sh&0x1f)==0: S(n, 0)
                else: S(n, (r[n]&0xffffffff)>>((~sh&0x1f)+1))
                return
            key=(m<<4)|d4
            def rot_l(v): return ((v<<1)|(v>>31))&0xffffffff
            def rot_r(v): return ((v>>1)|((v&1)<<31))&0xffffffff
            if key==0x00: self.t=(r[n]>>31)&1; S(n, r[n]<<1); return          # shll
            if key==0x01: self.t=r[n]&1; S(n, (r[n]&0xffffffff)>>1); return   # shlr
            if key==0x04: self.t=(r[n]>>31)&1; S(n, rot_l(r[n])); return      # rotl
            if key==0x05: self.t=r[n]&1; S(n, rot_r(r[n])); return            # rotr
            if key==0x24: c=self.t; self.t=(r[n]>>31)&1; S(n, (r[n]<<1)|c); return  # rotcl
            if key==0x25: c=self.t; self.t=r[n]&1; S(n, (r[n]>>1)|(c<<31)); return  # rotcr
            if key==0x20: self.t=(r[n]>>31)&1; S(n, r[n]<<1); return          # shal
            if key==0x21: self.t=r[n]&1; S(n, s32(r[n])>>1); return           # shar
            if key==0x08: S(n, r[n]<<2); return                              # shll2
            if key==0x09: S(n, (r[n]&0xffffffff)>>2); return                 # shlr2
            if key==0x18: S(n, r[n]<<8); return                              # shll8
            if key==0x19: S(n, (r[n]&0xffffffff)>>8); return                 # shlr8
            if key==0x28: S(n, r[n]<<16); return                             # shll16
            if key==0x29: S(n, (r[n]&0xffffffff)>>16); return                # shlr16
            if key==0x10: nv=(r[n]-1)&0xffffffff; S(n,nv); self.t=1 if nv==0 else 0; return  # dt
            if key==0x11: self.t=1 if s32(r[n])>=0 else 0; return            # cmp/pz
            if key==0x15: self.t=1 if s32(r[n])>0 else 0; return             # cmp/pl
            if key==0x0b: self.pr=(pc+4)&0xffffffff; dslot(r[n]); return      # jsr @rn
            if key==0x2b: dslot(r[n]); return                                # jmp @rn
            if key==0x0e: return                                             # ldc rn,sr (approx)
            if key==0x4e: self.spc=r[n]; return                               # ldc rn,spc
            if key==0x1e: self.gbr=r[n]; return                              # ldc rn,gbr
            if key==0x2a: self.pr=r[n]; return                               # lds rn,pr
            if key==0x0a: self.mach=r[n]; return                             # lds rn,mach
            if key==0x1a: self.macl=r[n]; return                             # lds rn,macl
            if key==0x22: S(n,r[n]-4); self.wr(r[n],self.pr,4); return     # sts.l pr,@-r15 (n=reg=15 usually)
            if key==0x02: S(n,r[n]-4); self.wr(r[n],self.mach,4); return   # sts.l mach
            if key==0x12: S(n,r[n]-4); self.wr(r[n],self.macl,4); return   # sts.l macl
            if key==0x26: self.pr=self.rd(r[n],4); S(n,r[n]+4); return        # lds.l @rn+,pr
            if key==0x06: self.mach=self.rd(r[n],4); S(n,r[n]+4); return      # lds.l @rn+,mach
            if key==0x16: self.macl=self.rd(r[n],4); S(n,r[n]+4); return      # lds.l @rn+,macl
            if key==0x07: S(n,r[n]+4); return                                # ldc.l @rn+,sr (approx)
            if key==0x17: self.gbr=self.rd(r[n],4); S(n,r[n]+4); return       # ldc.l @rn+,gbr
            if d4==0xe: 
                if key==0x1e: self.gbr=r[n]
                elif key==0x2e: self.vbr=r[n]
                # 0x0e sr,0x3e ssr,0x4e spc,0x5e mod... -> salva/ignora
                return                                                            # ldc rn,ctrl
            if d4==0x7:
                if key==0x17: self.gbr=self.rd(r[n],4)
                S(n,r[n]+4); return                                               # ldc.l @rn+,ctrl
            if d4==0x3:  # stc.l ctrl,@-rn (rari) -> push valore ctrl
                S(n,r[n]-4); self.wr(r[n], self.gbr if key==0x13 else 0, 4); return
            if d4==0x0 and m in (0,1,2,3,4):  # stc ctrl,rn
                S(n, self.gbr if m==1 else getattr(self,'vbr',0) if m==2 else 0); return
        raise Exception("opcode non gestito 0x%04x @0x%08x"%(w,pc))

    def run(self,stop_pc=None):
        self.steps=0
        fb=getattr(self,'forbid',None)
        while self.steps<self.maxsteps:
            if stop_pc is not None and self.pc==stop_pc: return True
            if fb and fb[0]<=self.pc<fb[1]: raise Exception('PC in zona vietata 0x%08x (alias senza cache?)'%self.pc)
            self.step(); self.steps+=1
        return False

if __name__=='__main__':
    import sys,zlib
    emu=SH(); flash=open('mpc500.bin','rb').read(); emu.map_flash(flash,0xA0000000)
    emu.map_ram(0x88000000,0x200000)   # RAM di lavoro
    # === VALIDAZIONE: esegui la CRC del firmware (0xA00043B8) su un buffer noto ===
    # metti un buffer di test in RAM a 0x88000000
    testdata=bytes(range(256))*4   # 1024 byte
    for i,b in enumerate(testdata): emu.wr(0x88000000+i,b,1)
    # CRC func: crc=(crc>>8)^tab[(crc^byte)&0xff]; init passato in r4? Il loop usa r6=crc iniziale.
    # dal reverse: entry 0xA00043B8: mov r4,r6; mov #0,r4; ... loop; ritorna r0=crc.
    # r5=buffer, r1=len, r4=init crc
    emu.r[4]=0xFFFFFFFF        # init crc (entry: mov r4,r6)
    emu.r[5]=0x88000000        # buffer  (entry: mov r5,r7)
    emu.r[6]=len(testdata)     # length  (entry: mov r6,r1)
    emu.pr=0xDEADBEEE          # ritorno sentinella (even)
    emu.pc=0xA00043B8
    ok=emu.run(stop_pc=0xDEADBEEE)
    got=emu.r[0]&0xffffffff
    ref_raw=zlib.crc32(testdata)&0xffffffff  # zlib = ~table_loop(init=FFFFFFFF)
    print("Emu CRC r0 = 0x%08x  (steps=%d, stopped=%s)"%(got,emu.steps,ok))
    print("zlib.crc32 = 0x%08x   ~got = 0x%08x"%(ref_raw,(~got)&0xffffffff))
    print("MATCH ~got==zlib:",((~got)&0xffffffff)==ref_raw,"  got==zlib:",got==ref_raw)

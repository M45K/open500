#!/usr/bin/env python3
# Mini-assembler SH (little-endian) per il framework di iniezione MPC500.
# Copre il sottoinsieme necessario a scrivere funzioni custom + trampolini.
import struct
class SHAsm:
    def __init__(self, base):
        self.base=base          # indirizzo runtime del primo byte
        self.code=[]            # lista di (kind, ...) risolti a fine
        self.words=[]           # opcodes 16-bit gia' pronti (o None placeholder)
        self.fixups=[]          # (index, kind, arg)
        self.labels={}
        self.pool=[]            # (value) costanti da mettere in fondo
        self.pool_refs=[]       # (word_index, value, reg)
        self.fixups8=[]         # (index, label) per bt/bf
        self.fixups12=[]        # (index, kind, label) per bra/bsr label
        self._pooled=set(); self._resolved=[]   # pool multiple (pool_here)
        self.pool_lfix=[]       # (word_index, label): literal = indirizzo di una label (risolto a fine)
        self.ns=''              # prefisso delle label (pages.add_page: piu' pagine nello stesso codice senza collisioni)
    def _n(self,name):
        # con un prefisso attivo ogni label con nome diventa ns+nome; quelle di Addon.newlabel ('__xxx<n>') sono gia' univoche
        if not self.ns or not isinstance(name,str): return name
        if name.startswith('P1:'): return 'P1:'+self._n(name[3:])
        if name.startswith('__') and name[-1:].isdigit(): return name
        return self.ns+name
    def _emit(self,w): self.words.append(w & 0xffff)
    def label(self,name): self.deflabel(name)
    # --- istruzioni ---
    def nop(self): self._emit(0x0009)
    def rts(self): self._emit(0x000b)
    def mov_imm(self,imm,rn):            # mov #imm,Rn  (8-bit segno)
        assert -128<=imm<=127; self._emit(0xE000|(rn<<8)|(imm&0xff))
    def mov_rm_rn(self,rm,rn): self._emit(0x6003|(rn<<8)|(rm<<4))  # mov Rm,Rn
    def movl_st_dec(self,rm,rn): self._emit(0x2006|(rn<<8)|(rm<<4)) # mov.l Rm,@-Rn
    def movl_ld_inc(self,rm,rn): self._emit(0x6006|(rn<<8)|(rm<<4)) # mov.l @Rm+,Rn
    def movl_st(self,rm,rn): self._emit(0x2002|(rn<<8)|(rm<<4))     # mov.l Rm,@Rn
    def movl_ld(self,rm,rn): self._emit(0x6002|(rn<<8)|(rm<<4))     # mov.l @Rm,Rn
    def stsl_pr(self): self._emit(0x4f22)     # sts.l pr,@-r15
    def ldsl_pr(self): self._emit(0x4f26)     # lds.l @r15+,pr
    def jsr(self,rn): self._emit(0x400b|(rn<<8))   # jsr @Rn
    def jmp(self,rn): self._emit(0x402b|(rn<<8))   # jmp @Rn
    def movb_st(self,rm,rn): self._emit(0x2000|(rn<<8)|(rm<<4))   # mov.b Rm,@Rn
    def movb_ld(self,rm,rn): self._emit(0x6000|(rn<<8)|(rm<<4))   # mov.b @Rm,Rn
    def dt(self,rn): self._emit(0x4010|(rn<<8))                   # dt Rn (decr, T=1 se 0)
    def tst_rr(self,rm,rn): self._emit(0x2008|(rn<<8)|(rm<<4))    # tst Rm,Rn
    def tst_imm(self,imm): self._emit(0xC800|(imm&0xff))          # tst #imm,r0
    def extu_b(self,rm,rn): self._emit(0x600C|(rn<<8)|(rm<<4))    # extu.b Rm,Rn
    def shll8(self,rn): self._emit(0x4018|(rn<<8))                # shll8 Rn
    def add_imm(self,imm,rn): self._emit(0x7000|(rn<<8)|(imm&0xff)) # add #imm,Rn
    def deflabel(self,name):
        name=self._n(name); assert name not in self.labels, 'label %s definita due volte' % name
        self.labels[name]=len(self.words)*2+self.base
    def bt_lbl(self,name):
        idx=len(self.words); self._emit(0x8900); self.fixups8.append((idx,self._n(name)))
    def bf_lbl(self,name):
        idx=len(self.words); self._emit(0x8B00); self.fixups8.append((idx,self._n(name)))
    def movl_pool(self,value,rn):
        # mov.l @(disp,PC),Rn ; disp risolto a fine verso la pool (l'ultima pool_here() successiva, o quella finale)
        # value puo' essere il NOME di una label: il literal sara' il suo indirizzo
        idx=len(self.words); self._emit(0xD000|(rn<<8))  # disp placeholder
        self.pool_refs.append((idx,self._n(value),rn))
    def pool_here(self):
        """Emette QUI una literal pool con le costanti referenziate finora (non ancora risolte). Va chiamata
        subito dopo un salto incondizionato (bra/rts): i literal non devono essere eseguiti. Permette codice > 1KB."""
        pending=[r for r in self.pool_refs if r not in self._pooled]
        if not pending: return
        while (len(self.words)*2+self.base)&3: self._emit(0x0009)
        poolmap={}
        for (idx,value,rn) in pending:
            if value not in poolmap:
                poolmap[value]=len(self.words)*2+self.base
                if isinstance(value,str):   # label: placeholder, risolto in assemble()
                    self.pool_lfix.append((len(self.words),value)); self._emit(0); self._emit(0)
                else:
                    self._emit(value&0xffff); self._emit((value>>16)&0xffff)
            self._resolved.append((idx,value,poolmap[value])); self._pooled.add((idx,value,rn))
    def rotl(self,rn): self._emit(0x4004|(rn<<8))               # rotl Rn
    def shlr8(self,rn): self._emit(0x4019|(rn<<8))              # shlr8 Rn
    def shll16(self,rn): self._emit(0x4028|(rn<<8))             # shll16 Rn (<<16)
    def shlr16(self,rn): self._emit(0x4029|(rn<<8))             # shlr16 Rn
    def cmpgt(self,rm,rn): self._emit(0x3007|(rn<<8)|(rm<<4))    # cmp/gt Rm,Rn
    def cmpeq(self,rm,rn): self._emit(0x3000|(rn<<8)|(rm<<4))    # cmp/eq Rm,Rn
    def cmphs(self,rm,rn): self._emit(0x3002|(rn<<8)|(rm<<4))    # cmp/hs Rm,Rn
    def cmphi(self,rm,rn): self._emit(0x3006|(rn<<8)|(rm<<4))    # cmp/hi Rm,Rn (T = Rn > Rm senza segno)
    def movb_ldinc(self,rm,rn): self._emit(0x6004|(rn<<8)|(rm<<4)) # mov.b @Rm+,Rn
    def movw_ld(self,rm,rn): self._emit(0x6001|(rn<<8)|(rm<<4))   # mov.w @Rm,Rn
    def movw_st(self,rm,rn): self._emit(0x2001|(rn<<8)|(rm<<4))   # mov.w Rm,@Rn
    def movt(self,rn): self._emit(0x0029|(rn<<8))                 # movt Rn (Rn=T)
    def xor_rr(self,rm,rn): self._emit(0x200A|(rn<<8)|(rm<<4))    # xor Rm,Rn
    def movw_ldinc(self,rm,rn): self._emit(0x6005|(rn<<8)|(rm<<4))          # mov.w @Rm+,Rn
    def movl_ld_disp(self,rm,disp,rn): assert 0<=disp<=60 and disp%4==0, 'mov.l @(disp): 0..60 multiplo di 4, non %r'%disp; self._emit(0x5000|(rn<<8)|(rm<<4)|((disp>>2)&0xf))  # mov.l @(disp,Rm),Rn (disp 0..60)
    def movl_st_disp(self,rm,disp,rn): assert 0<=disp<=60 and disp%4==0, 'mov.l @(disp): 0..60 multiplo di 4, non %r'%disp; self._emit(0x1000|(rn<<8)|(rm<<4)|((disp>>2)&0xf))  # mov.l Rm,@(disp,Rn)
    def sett(self): self._emit(0x0018)                                   # sett (T=1)
    def clrt(self): self._emit(0x0008)                                   # clrt (T=0)
    def or_rr(self,rm,rn): self._emit(0x200B|(rn<<8)|(rm<<4))            # or Rm,Rn
    def shll(self,rn): self._emit(0x4000|(rn<<8))                  # shll Rn (T=MSB)
    def rotcl(self,rn): self._emit(0x4024|(rn<<8))                 # rotcl Rn (T<-MSB, LSB<-T)
    def mul_l(self,rm,rn): self._emit(0x0007|(rn<<8)|(rm<<4))       # mul.l Rm,Rn -> MACL
    def sts_macl(self,rn): self._emit(0x001A|(rn<<8))              # sts macl,Rn
    def dmulu_l(self,rm,rn): self._emit(0x3005|(rn<<8)|(rm<<4))     # dmulu.l Rm,Rn -> MACH:MACL (64 bit senza segno)
    def sts_mach(self,rn): self._emit(0x000A|(rn<<8))              # sts mach,Rn
    def lds_pr(self,rm): self._emit(0x402A|(rm<<8))                # lds Rm,PR
    def stc_sr(self,rn): self._emit(0x0002|(rn<<8))                # stc SR,Rn
    def ldc_sr(self,rm): self._emit(0x400E|(rm<<8))                # ldc Rm,SR
    def ldc_spc(self,rm): self._emit(0x404E|(rm<<8))               # ldc Rm,SPC
    def rte(self): self._emit(0x002B)                               # rte
    def movl_st_gbr(self,disp): assert disp%4==0 and 0<=disp<1024; self._emit(0xC200|(disp//4))   # mov.l r0,@(disp,GBR)
    def sts_pr(self,rn): self._emit(0x002A|(rn<<8))                # sts PR,Rn
    def cmpge(self,rm,rn): self._emit(0x3003|(rn<<8)|(rm<<4))       # cmp/ge Rm,Rn (signed Rn>=Rm)
    def cmppl(self,rn): self._emit(0x4015|(rn<<8))                 # cmp/pl Rn (Rn>0)
    def cmppz(self,rn): self._emit(0x4011|(rn<<8))                 # cmp/pz Rn (Rn>=0)
    def movb_st_disp(self,disp,rn): assert 0<=disp<=15, 'mov.b @(disp): 0..15, non %r'%disp; self._emit(0x8000|(rn<<4)|(disp&0xf))   # mov.b r0,@(disp,Rn)
    def movb_ld_disp(self,rm,disp): assert 0<=disp<=15, 'mov.b @(disp): 0..15, non %r'%disp; self._emit(0x8400|(rm<<4)|(disp&0xf))   # mov.b @(disp,Rm),r0
    def neg(self,rm,rn): self._emit(0x600B|(rn<<8)|(rm<<4))         # neg Rm,Rn
    def not_rr(self,rm,rn): self._emit(0x6007|(rn<<8)|(rm<<4))    # not Rm,Rn
    def and_rr(self,rm,rn): self._emit(0x2009|(rn<<8)|(rm<<4))    # and Rm,Rn
    def sub(self,rm,rn): self._emit(0x3008|(rn<<8)|(rm<<4))      # sub Rm,Rn (Rn-=Rm)
    def add_rr(self,rm,rn): self._emit(0x300C|(rn<<8)|(rm<<4))   # add Rm,Rn (Rn+=Rm)
    def extu_w(self,rm,rn): self._emit(0x600D|(rn<<8)|(rm<<4))   # extu.w Rm,Rn
    def exts_w(self,rm,rn): self._emit(0x600F|(rn<<8)|(rm<<4))   # exts.w Rm,Rn
    def muls_w(self,rm,rn): self._emit(0x200F|(rn<<8)|(rm<<4))   # muls.w Rm,Rn -> MACL (s16*s16)
    def shad(self,rm,rn): self._emit(0x400C|(rn<<8)|(rm<<4))     # shad Rm,Rn (Rm>=0: Rn<<=Rm, Rm<0: Rn>>=-Rm aritmetico)
    def shar(self,rn): self._emit(0x4021|(rn<<8))                # shar Rn
    def bra_lbl(self,name):
        idx=len(self.words); self._emit(0xA000); self.fixups12.append((idx,'bra',self._n(name)))
    def bsr_lbl(self,name):
        idx=len(self.words); self._emit(0xB000); self.fixups12.append((idx,'bsr',self._n(name)))
    def cmp_imm(self,imm): self._emit(0x8800|(imm&0xff))          # cmp/eq #imm,r0
    def cmp_hs_imm_via(self):
        pass
    def shll2(self,rn): self._emit(0x4008|(rn<<8))               # shll2 Rn (<<2)
    def shlr2(self,rn): self._emit(0x4009|(rn<<8))               # shlr2 Rn
    def shlr(self,rn): self._emit(0x4001|(rn<<8))                # shlr Rn
    def or_imm(self,imm): self._emit(0xCB00|(imm&0xff))          # or #imm,r0
    def and_imm(self,imm): self._emit(0xC900|(imm&0xff))         # and #imm,r0
    def mov_rn(self,rm,rn): self._emit(0x6003|(rn<<8)|(rm<<4))   # mov Rm,Rn (alias)
    def extub2(self,rm,rn): self._emit(0x600C|(rn<<8)|(rm<<4))   # extu.b (alias)
    def bra_to(self,target):
        idx=len(self.words); self._emit(0xA000)
        self.fixups.append((idx,'bra',target))
    def bsr_to(self,target):
        idx=len(self.words); self._emit(0xB000)
        self.fixups.append((idx,'bsr',target))
    def assemble(self):
        # posiziona la literal pool dopo il codice, allineata a 4
        while (len(self.words)*2+self.base)&3: self._emit(0x0009)  # pad nop
        self.pool_here()   # pool finale per i literal rimasti
        # risolvi mov.l @(disp,PC)
        for (idx,value,addr) in self._resolved:
            pc=idx*2+self.base
            disp=(addr-((pc+4)&~3))//4
            assert 0<=disp<=255,'pool troppo lontana disp=%d (usa pool_here() dopo un bra/rts)'%disp
            self.words[idx]=(self.words[idx]&0xff00)|disp
        # risolvi bra/bsr
        for (idx,kind,target) in self.fixups:
            pc=idx*2+self.base
            disp=(target-(pc+4))//2
            assert -2048<=disp<=2047,'branch fuori range %d'%disp
            self.words[idx]=(self.words[idx]&0xf000)|(disp&0x0fff)
        for (idx,kind,name) in self.fixups12:
            pc=idx*2+self.base
            disp=(self.labels[name]-(pc+4))//2
            assert -2048<=disp<=2047,'bra/bsr lbl %s fuori range %d (pc %#x)'%(name,disp,pc)
            self.words[idx]=(self.words[idx]&0xf000)|(disp&0x0fff)
        for (idx,name) in self.fixups8:
            pc=idx*2+self.base
            disp=(self.labels[name]-(pc+4))//2
            assert -128<=disp<=127,'bt/bf fuori range %d'%disp
            self.words[idx]=(self.words[idx]&0xff00)|(disp&0xff)
        for (idx,name) in self.pool_lfix:
            v=self.labels[name[3:]]&0xDFFFFFFF if name.startswith('P1:') else self.labels[name]   # 'P1:lbl' = alias in cache
            self.words[idx]=v&0xffff; self.words[idx+1]=(v>>16)&0xffff
        return b''.join(struct.pack('<H',w) for w in self.words)
if __name__=='__main__':
    # self-test: funzione che scrive 0x0000CAFE a 0x88400000 e ritorna
    a=SHAsm(0xA006ADE4)
    a.movl_st_dec(1,15); a.movl_st_dec(2,15)   # push r1,r2
    a.movl_pool(0x88400000,1)
    a.movl_pool(0x0000CAFE,2)
    a.movl_st(2,1)                              # *r1 = r2
    a.movl_ld_inc(15,2); a.movl_ld_inc(15,1)   # pop r2,r1
    a.rts(); a.nop()
    blob=a.assemble()
    open('/tmp/selftest.bin','wb').write(blob)
    print("assemblati %d byte @0x%08x"%(len(blob),a.base))

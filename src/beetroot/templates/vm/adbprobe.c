#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
/* usage: adbprobe <ipver:4|6> <addr> <port> */
int main(int argc,char**argv){
  if(argc<4){printf("usage\n");return 2;}
  int v6=atoi(argv[1])==6;
  int fd=socket(v6?AF_INET6:AF_INET,SOCK_STREAM,0);
  if(fd<0){perror("sock");return 3;}
  struct timeval tv={8,0}; setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&tv,sizeof tv);
  int rc;
  if(v6){struct sockaddr_in6 a={0};a.sin6_family=AF_INET6;a.sin6_port=htons(atoi(argv[3]));inet_pton(AF_INET6,argv[2],&a.sin6_addr);rc=connect(fd,(void*)&a,sizeof a);}
  else{struct sockaddr_in a={0};a.sin_family=AF_INET;a.sin_port=htons(atoi(argv[3]));inet_pton(AF_INET,argv[2],&a.sin_addr);rc=connect(fd,(void*)&a,sizeof a);}
  if(rc){perror("connect");return 4;}
  /* adb CNXN */
  unsigned int cmd=0x4e584e43,a0=0x01000001,a1=256*1024;
  const char*sysid="host::\0"; unsigned len=strlen(sysid)+1; unsigned crc=0; for(unsigned i=0;i<len;i++)crc+=(unsigned char)sysid[i];
  unsigned int hdr[6]={cmd,a0,a1,len,crc,cmd^0xffffffff};
  write(fd,hdr,sizeof hdr); write(fd,sysid,len);
  unsigned char buf[256]; int n=read(fd,buf,sizeof buf);
  if(n<=0){printf("PROBE: connected but NO REPLY (n=%d)\n",n);return 5;}
  printf("PROBE: REPLY n=%d first4=%c%c%c%c\n",n,buf[0],buf[1],buf[2],buf[3]);
  return 0;
}

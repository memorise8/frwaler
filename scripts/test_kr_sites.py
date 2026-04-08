#!/usr/bin/env python3
"""Test Korean government/research URLs with multiple fetch methods.

Usage:
    PYTHONUNBUFFERED=1 python scripts/test_kr_sites.py [timeout_seconds]
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import cloudscraper
from playwright.sync_api import sync_playwright

KR_URLS = [
    # 보건복지부 (MOHW)
    ("mohw/research", "https://www.mohw.go.kr/board.es?mid=a10411010200&bid=0019&cg_code=C01"),
    ("mohw/stats", "https://www.mohw.go.kr/board.es?mid=a10411010300&bid=0019&cg_code=C02"),
    ("mohw/whitepaper", "https://www.mohw.go.kr/board.es?mid=a10503010100&bid=0027"),

    # 기획재정부 (MOEF)
    ("mofe/policy", "https://mofe.go.kr/id/pdo.do?menuNo=3050000"),
    ("moef/news", "https://www.moef.go.kr/nw/nes/nesdta.do?bbsId=MOSFBBS_000000000028&menuNo=4010100"),

    # 국세청 (NTS)
    ("nts/stats", "https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?mi=6690&cntntsId=8102"),
    ("nts/announce", "https://www.nts.go.kr/nts/na/ntt/selectNttList.do?mi=2201&bbsId=1028"),
    ("nts/data", "https://www.nts.go.kr/nts/na/ntt/selectNttList.do?mi=2210&bbsId=1034"),

    # 조달청 (PPS)
    ("pps/press", "https://www.pps.go.kr/kor/bbs/list.do?key=00634"),
    ("pps/data", "https://www.pps.go.kr/kor/bbs/list.do?key=00664"),

    # 통계청 (KOSTAT)
    ("kostat/press", "https://www.kostat.go.kr/board.es?mid=a10301010000&bid=a103010100&ref_bid=203,204,205,206,207,210,211,11109,11113,11814,213,215,214,11860,11695,216,218,219,220,10820,11815,11895,11816,208,245,222,223,225,226,227,228,229,230,11321,232,233,234,12029,10920,11469,11470,11817,236,237,11471,238,240,241,11865,243,244,11893,11898,12031,11825,246,0067"),
    ("kostat/pub", "https://www.kostat.go.kr/board.es?mid=a10403010000&bid=1401"),
    ("kostat/notice", "https://www.kostat.go.kr/board.es?mid=a10405000000&bid=1601"),

    # 교육부 (MOE)
    ("moe/press", "https://www.moe.go.kr/boardCnts/listRenew.do?boardID=294&m=020402&s=moe"),
    ("moe/notice", "https://www.moe.go.kr/boardCnts/listRenew.do?boardID=72729&m=020404&s=moe"),
    ("moe/stats1", "https://www.moe.go.kr/boardCnts/listRenew.do?boardID=346&renew=346&m=041201&s=moe"),
    ("moe/stats2", "https://www.moe.go.kr/boardCnts/listRenew.do?boardID=345&m=041202&s=moe"),
    ("moe/stats3", "https://www.moe.go.kr/boardCnts/listRenew.do?boardID=344&renew=344&m=041203&s=moe"),
    ("moe/stats4", "https://www.moe.go.kr/boardCnts/listRenew.do?boardID=431&m=041204&s=moe"),
    ("moe/stats5", "https://www.moe.go.kr/boardCnts/listRenew.do?boardID=428&m=041205&s=moe"),

    # 과학기술정보통신부 (MSIT)
    ("msit/press", "https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=47&mId=65"),
    ("msit/announce", "https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=243&mId=244"),
    ("msit/laws", "https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=100&mId=101"),
    ("msit/laws2", "https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=100&mId=102"),
    ("msit/data", "https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=74&mId=99"),
    ("msit/stats1", "https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=208&mId=307"),
    ("msit/stats2", "https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=208&mId=309"),

    # 우주항공청 (KASA)
    ("kasa/notice", "https://www.kasa.go.kr/bbs/BBSMSTR_000000000010/list.do"),

    # 외교부 (MOFA)
    ("mofa/press", "https://www.mofa.go.kr/www/brd/m_21375/list.do"),
    ("mofa/treaty", "https://www.mofa.go.kr/www/brd/m_3828/list.do"),
    ("mofa/treaty2", "https://www.mofa.go.kr/www/brd/m_3988/list.do"),
    ("mofa/un", "https://www.mofa.go.kr/www/brd/m_3993/list.do"),
    ("mofa/intl", "https://www.mofa.go.kr/www/brd/m_20152/list.do"),
    ("mofa/bilateral", "https://www.mofa.go.kr/www/brd/m_4008/list.do"),
    ("mofa/economy1", "https://www.mofa.go.kr/www/brd/m_4048/list.do"),
    ("mofa/economy2", "https://www.mofa.go.kr/www/brd/m_4049/list.do"),
    ("mofa/dev", "https://www.mofa.go.kr/www/brd/m_26799/list.do"),
    ("mofa/security1", "https://www.mofa.go.kr/www/brd/m_4076/list.do"),
    ("mofa/security2", "https://www.mofa.go.kr/www/brd/m_4078/list.do"),
    ("mofa/security3", "https://www.mofa.go.kr/www/brd/m_4080/list.do"),
    ("mofa/consular", "https://www.mofa.go.kr/www/brd/m_4099/list.do"),

    # 국가보훈부 (OKA)
    ("oka/notice", "https://www.oka.go.kr/web/board/brdList.do?menu_cd=000018"),
    ("oka/data", "https://www.oka.go.kr/web/board/brdList.do?menu_cd=000164"),

    # 통일부 (Unification)
    ("unikorea/press", "https://www.unikorea.go.kr/web/unikorea/bbs/bbs_0000000000000004"),
    ("unikorea/data", "https://www.unikorea.go.kr/web/unikorea/bbs/bbs_0000000000000181"),

    # 법무부 (MOJ)
    ("moj/press", "https://www.moj.go.kr/moj/418/subview.do"),
    ("moj/data", "https://www.moj.go.kr/moj/221/subview.do"),

    # 검찰청 (SPO)
    ("spo/data", "https://www.spo.go.kr/site/spo/ex/board/List.do?cbIdx=1303"),

    # 사법연수원 (IOJ)
    ("ioj/library", "https://book.ioj.go.kr/library/10210/search?materialTypes=bc&categoryTab=true"),
    ("ioj/research", "https://book.ioj.go.kr/library/10221/search?materialTypes=bc&categoryId=12006"),

    # 국방부 (MND)
    ("mnd/press", "https://www.mnd.go.kr/user/newsInUserRecord.action?siteId=mnd&handle=I_669&id=mnd_020500000000"),

    # 병무청 (MMA)
    ("mma/data", "https://www.mma.go.kr/board/boardList.do?gesipan_id=15&mc=mma0000392"),

    # 방위사업청 (DAPA)
    ("dapa/press", "https://www.dapa.go.kr/dapa/doc/selectDocList.do?menuSeq=3069&bbsSeq=326"),

    # 행정안전부 (MOIS)
    ("mois/press", "https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardList.do?bbsId=BBSMSTR_000000000008"),
    ("mois/data1", "https://www.mois.go.kr/frt/bbs/type001/commonSelectBoardList.do?bbsId=BBSMSTR_000000000013"),
    ("mois/data2", "https://www.mois.go.kr/frt/bbs/type001/commonSelectBoardList.do?bbsId=BBSMSTR_000000000012"),

    # 경찰청 (Police)
    ("police/press", "https://www.police.go.kr/user/bbs/BD_selectBbsList.do?q_bbsCode=1113"),
    ("police/data", "https://www.police.go.kr/user/bbs/BD_selectBbsList.do?q_bbsCode=1002"),

    # 소방청 (NFA)
    ("nfa/press", "https://www.nfa.go.kr/nfa/news/pressrelease/press/"),
    ("nfa/research", "https://www.nfa.go.kr/nfa/publicrelations/policyarchive/policyresearch/;jsessionid=9enQl6h5uCyjt+EqFr1EfMxa.nfa21"),
    ("nfa/stats", "https://www.nfa.go.kr/nfa/releaseinformation/statisticalinformation/main/"),

    # 국가보훈처 (MPVA)
    ("mpva/press", "https://www.mpva.go.kr/mpva/selectBbsNttList.do?bbsNo=75&key=210"),
    ("mpva/data", "https://www.mpva.go.kr/mpva/selectBbsNttList.do?bbsNo=16&key=77"),

    # 국가유산청 (KHS)
    ("khs/press", "https://www.khs.go.kr/newsBbz/selectNewsBbzList.do?sectionId=all_sec_1&mn=NS_01_02"),
    ("khs/data", "https://www.khs.go.kr/cop/bbs/selectBoardList.do?bbsId=BBSMSTR_1020&mn=NS_03_07_04"),

    # 농림축산식품부 (MAFRA)
    ("mafra/press", "https://www.mafra.go.kr/home/5109/subview.do"),

    # 농촌진흥청 (RDA)
    ("rda/report", "https://www.rda.go.kr/board/reformBoard.do?mode=html&prgId=ref_controlReport"),
    ("rda/farm", "https://www.rda.go.kr/board/board.do?mode=list&prgId=day_farmprmninfoEntry"),

    # 산림청 (Forest)
    ("forest/press", "https://www.forest.go.kr/kfsweb/cop/bbs/selectBoardList.do?bbsId=BBSMSTR_1036&mn=NKFS_04_02_01"),
    ("forest/data", "https://kfss.forest.go.kr/stat/ptl/article/articleList.do?curMenu=9795&bbsId=ptlPdsBase"),
    ("forest/pub", "https://kna.forest.go.kr/kfsweb/kfi/kfs/kna/application/publication/list.do?mainCd=210103&mn=UKNA_04_10"),

    # 산업통상자원부 (MOTIE)
    ("motie/press", "https://www.motie.go.kr/kor/article/ATCL3f49a5a8c"),
    ("motie/data", "https://www.motir.go.kr/kor/article/ATCL449580de4"),

    # 특허청 (KIPO)
    ("kipo/press", "https://www.kipo.go.kr/ko/kpoBultnMgmt.do?menuCd=SCD0200618&parntMenuCd2=SCD0200052"),
    ("kipo/stats", "https://www.kipo.go.kr/ko/kpoBultnMgmt.do?menuCd=SCD0201112&sysCd=SCD02&pgmId=BUT0000065"),
    ("kipo/data", "https://www.kipo.go.kr/ko/kpoBultnMgmt.do?menuCd=SCD0200640&parntMenuCd2=SCD0200281"),

    # 질병관리청 (KDCA)
    ("kdca/press", "https://www.kdca.go.kr/kdca/2847/subview.do"),

    # 문화체육관광부 (MCEE)
    ("mcee/press", "https://www.mcee.go.kr/home/web/index.do?menuId=10525"),

    # 기상청 (KMA)
    ("kma/press", "https://www.kma.go.kr/kma/news/press_01.jsp?from=2022-10-06&to=2023-10-06&field=subject&text="),

    # 여성가족부 (MOGEF)
    ("mogef/press", "https://www.mogef.go.kr/nw/rpd/nw_rpd_s001.do?mid=news405"),

    # 국토교통부 (MOLIT)
    ("molit/press", "https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp"),

    # 국가기록원 (NAACC)
    ("naacc/notice", "https://naacc.go.kr/WEB/contents/N4020000000.do"),
    ("naacc/data", "https://naacc.go.kr/WEB/contents/N3050100000.do"),

    # PRISM 정책연구
    ("prism/search", "https://www.prism.go.kr/homepage/prtl/totalsearch/list"),

    # 새만금개발청
    ("saemangeum/press", "https://www.saemangeum.go.kr/sda/brd/list.do?key=2009074409621"),

    # 해양수산부 (MOF)
    ("mof/press", "https://www.mof.go.kr/doc/ko/selectDocList.do?menuSeq=971&bbsSeq=10&listUpdtDt=2025-07-03++10%3A00"),

    # 해양경찰청 (KCG)
    ("kcg/press", "https://www.kcg.go.kr/kcg/na/ntt/selectNttList.do?mi=2799&bbsId=313"),

    # 중소벤처기업부 (MSS)
    ("mss/press", "https://www.mss.go.kr/site/smba/ex/bbs/List.do?cbIdx=86"),
    ("mss/stats", "https://www.mss.go.kr/site/smba/foffice/ex/statDB/stReportRoList.do?gb=1&nodeId=&roCode="),

    # 인사혁신처 (OPM)
    ("opm/press", "https://www.opm.go.kr/opm/news/press-release.do"),
    ("opm/policy", "https://www.opm.go.kr/opm/info/policies.do"),

    # 식품의약품안전처 (MFDS)
    ("mfds/press", "https://www.mfds.go.kr/brd/m_99/list.do"),

    # 공정거래위원회 (FTC)
    ("ftc/press", "https://www.ftc.go.kr/www/selectBbsNttList.do?bordCd=3&key=12&searchCtgry=01,02"),
    ("ftc/data", "https://www.ftc.go.kr/www/selectBbsNttList.do?bordCd=103&key=190"),

    # 금융위원회 (FSC)
    ("fsc/press", "https://www.fsc.go.kr/no010101"),

    # 금융감독원 (FSS)
    ("fss/press", "https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218"),

    # 한국은행 (BOK)
    ("bok/data1", "https://www.bok.or.kr/portal/singl/newsData/list.do?pageIndex=1&targetDepth=&menuNo=201150&syncMenuChekKey=1&depthSubMain=&subMainAt=&searchCnd=1&searchKwd=&depth2=200038&date=&sdate=&edate=&sort=1&pageUnit=10"),
    ("bok/data2", "https://www.bok.or.kr/portal/singl/newsData/list.do?pageIndex=1&targetDepth=&menuNo=201150&syncMenuChekKey=3&depthSubMain=&subMainAt=&searchCnd=1&searchKwd=&depth2=200699&date=&sdate=&edate=&sort=1&pageUnit=10"),
    ("bok/data3", "https://www.bok.or.kr/portal/singl/newsData/list.do?pageIndex=1&targetDepth=&menuNo=201150&syncMenuChekKey=5&depthSubMain=&subMainAt=&searchCnd=1&searchKwd=&depth2=201156&date=&sdate=&edate=&sort=1&pageUnit=10"),

    # 한국재정정보원 (KINFA)
    ("kinfa/press", "https://www.kinfa.or.kr/notificationPromotion/news.do"),

    # 한국자산관리공사 (KAMCO)
    ("kamco/press", "https://www.kamco.or.kr/portal/bbs/list.do?ptIdx=282&mId=0701030000"),
    ("kamco/data", "https://www.kamco.or.kr/portal/bbs/list.do?ptIdx=284&mId=0705010000"),

    # 근로복지공단 (COMWEL)
    ("comwel/press", "https://www.comwel.or.kr/comwel/noti/pres.jsp"),
    ("comwel/stats", "https://www.comwel.or.kr/comwel/info/data/stat/stat.jsp"),

    # 국민연금공단 (NPS)
    ("nps/data", "https://www.nps.or.kr/inforls/publdata/getOHAB0019M0List.do?menuId=MN24000873"),
    ("nps/coverage", "https://www.nps.or.kr/pnsgdnc/nscvrgdata/getOHAE0002M0List.do?menuId=MN24000898"),

    # 국민건강보험공단 (NHIS)
    ("nhis/press", "https://www.nhis.or.kr/nhis/together/wbhaec07800m01.do"),
    ("nhis/data", "https://www.nhis.or.kr/nhis/together/wbhaec06700m01.do"),

    # 한국조세재정연구원 (KIPF)
    ("kipf/report", "https://www.kipf.re.kr/kor/Publication/KipfReport/kiPublish/CA/list.do"),
    ("kipf/journal", "https://www.kipf.re.kr/kor/Publication/KipfPeriodicals/kiPublish/CB/list.do"),
    ("kipf/other", "https://www.kipf.re.kr/kor/Publication/KipfOtherData/kiPublish/CC/list.do"),
    ("kipf/center1", "https://www.kipf.re.kr/kor/Publication/CenterReport/kiPublish/CA/Center/list.do"),
    ("kipf/center2", "https://www.kipf.re.kr/kor/Publication/CenterPeriodicals/kiPublish/CB/Center/list.do"),
    ("kipf/press", "https://www.kipf.re.kr/bbs/kor_Plaza_PressRelease.do"),
    ("kipf/policy1", "https://www.kipf.re.kr/kor/Publication/PolicyResearch/kiPublish/CD3/list.do"),
    ("kipf/policy2", "https://www.kipf.re.kr/kor/Publication/PolicyReport/kiPublish/CD4/list.do"),

    # 경제인문사회연구회 (NRC)
    ("nrc/report", "https://www.nrc.re.kr/board.es?mid=a10301000000&bid=0008&publication_p_cd=RSPX002"),
    ("nrc/press", "https://www.nrc.re.kr/board.es?mid=a12102000000&bid=0015"),

    # 대외경제정책연구원 (KIEP)
    ("kiep/pub", "https://www.kiep.go.kr/gallery.es?mid=a10101010000&bid=0001&cg_code=C03%2CC05%2CC02%2CC13%2CC01%2CC19%2CC17%2CC11%2CC20"),
    ("kiep/press", "https://www.kiep.go.kr/board.es?mid=a10503000000&bid=0025"),

    # 통일연구원 (KINU)
    ("kinu/report", "https://www.kinu.or.kr/main/module/report/index.do?nav_code=mai1674786094"),
    ("kinu/press", "https://www.kinu.or.kr/main/board/index.do?nav_code=mai1674793705&code=XDXh8TgkVFNJ"),

    # 한국형사법무정책연구원 (KICJ)
    ("kicj/report", "https://www.kicj.re.kr/board.es?mid=a10101000000&bid=0001"),
    ("kicj/press", "https://www.kicj.re.kr/board.es?mid=a10304010000&bid=0012"),

    # 한국행정연구원 (KIPA)
    ("kipa/pub", "https://www.kipa.re.kr/html/kor/rsch/pblc/pblcDataTab.do"),
    ("kipa/press", "https://www.kipa.re.kr/html/kor/cmn/bbs/cmnBbsGenList.do/237"),
    ("kipa/data", "https://www.kipa.re.kr/html/kor/rsch/rsd/rschData.do"),

    # 한국교육과정평가원 (KICE)
    ("kice/report", "https://www.kice.re.kr/boardCnts/list.do?boardID=10024&m=050102&s=kice&searchStr="),

    # 에너지경제연구원 (KEEI)
    ("keei/report", "https://www.keei.re.kr/board.es?mid=a10101010000&bid=0001"),
    ("keei/journal", "https://www.keei.re.kr/board.es?mid=a10102010000&bid=0002"),
    ("keei/press", "https://www.keei.re.kr/board.es?mid=a10202020000&bid=0008"),

    # 정보통신정책연구원 (KISDI)
    ("kisdi/press", "https://www.kisdi.re.kr/bbs/list.do?key=m2101113055776"),
    ("kisdi/report1", "https://www.kisdi.re.kr/report/list.do?key=m2101113024153&arrMasterId=3934560"),
    ("kisdi/report2", "https://www.kisdi.re.kr/report/list.do?key=m2101113025536&arrMasterId=3934550"),
    ("kisdi/report3", "https://www.kisdi.re.kr/report/list.do?key=m2309145696871&arrMasterId=5850223"),
    ("kisdi/bbs", "https://www.kisdi.re.kr/bbs/list.do?key=m2101113055776"),

    # 한국보건사회연구원 (KIHASA)
    ("kihasa/report", "https://www.kihasa.re.kr/publish/report/all/list"),
    ("kihasa/press", "https://www.kihasa.re.kr/news/press/list"),

    # 육아정책연구소 (KICCE)
    ("kicce/report", "https://www.kicce.re.kr/main/board/index.do?menu_idx=321&board_idx=0&manage_idx=102&old_menu_idx=0&old_manage_idx=0&old_board_idx=0&group_depth=0&parent_idx=0&group_idx=0&group_ord=0&viewMode=NORMAL&authKey=&search_text=&rowCount=10&viewPage=1"),
    ("kicce/journal", "https://www.kicce.re.kr/main/board/index.do?menu_idx=231&manage_idx=103"),
    ("kicce/issue", "https://www.kicce.re.kr/main/board/index.do?menu_idx=35&manage_idx=43"),
    ("kicce/press", "https://www.kicce.re.kr/main/board/index.do?menu_idx=95&manage_idx=56"),

    # 한국노동연구원 (KLI)
    ("kli/report", "https://www.kli.re.kr/kli/rschRptpList.es?mid=a10102060000"),
    ("kli/journal", "https://www.kli.re.kr/kli/prdclList.es?mid=a10103050000"),
    ("kli/press", "https://www.kli.re.kr/kli/nscvrgList.es?mid=a10305000000"),

    # 한국직업능력연구원 (KRIVET)
    ("krivet/report1", "https://www.krivet.re.kr/kor/sub.do?menuSn=12"),
    ("krivet/report2", "https://www.krivet.re.kr/kor/sub.do?menuSn=15"),
    ("krivet/report3", "https://www.krivet.re.kr/kor/sub.do?menuSn=19"),
    ("krivet/report4", "https://www.krivet.re.kr/kor/sub.do?menuSn=23"),

    # 한국해양수산개발원 (KMI)
    ("kmi/report", "https://www.kmi.re.kr/web/board/list.do?rbsIdx=384"),
    ("kmi/data", "https://www.kmi.re.kr/web/contents/contentsView.do?rbsIdx=221"),
    ("kmi/books", "https://www.kmi.re.kr/web/trebook/list.do?rbsIdx=273"),
    ("kmi/press", "https://www.kmi.re.kr/web/board/list.do?rbsIdx=164"),

    # 한국법제연구원 (KLRI)
    ("klri/pub", "https://www.klri.re.kr/kor/publication/list.do"),
    ("klri/journal1", "https://www.klri.re.kr/kor/journal/Z/list.do"),
    ("klri/journal2", "https://www.klri.re.kr/kor/journal/N/list.do"),
    ("klri/issue1", "https://www.klri.re.kr/kor/issueData/P/list.do"),
    ("klri/law", "https://www.klri.re.kr/kor/lawData/list.do"),
    ("klri/issue2", "https://www.klri.re.kr/kor/issueData/S/list.do"),
    ("klri/data", "https://www.klri.re.kr/kor/data/W/list.do"),
    ("klri/issue3", "https://www.klri.re.kr/kor/issueData/B/list.do"),
    ("klri/journal3", "https://www.klri.re.kr/kor/journal/A/list.do"),
    ("klri/issue4", "https://www.klri.re.kr/kor/issueData/A/list.do"),
    ("klri/press", "https://www.klri.re.kr/kor/bbs/BBSMSTR_000000000002/list.do"),

    # 한국여성정책연구원 (KWDI)
    ("kwdi/report", "https://www.kwdi.re.kr/publications/report.do"),
    ("kwdi/journal", "https://www.kwdi.re.kr/publications/journal.do"),
    ("kwdi/review", "https://www.kwdi.re.kr/publications/genderReview.do"),
    ("kwdi/brief", "https://www.kwdi.re.kr/publications/kwdiBrief.do"),
    ("kwdi/press", "https://www.kwdi.re.kr/plaza/bodo.do"),

    # 한국청소년정책연구원 (NYPI)
    ("nypi/report", "https://www.nypi.re.kr/mps/rpstr/rep/list?menuId=MENU002040301010000"),
    ("nypi/press", "https://www.nypi.re.kr/board?menuKey=uCjzEQTnJu&bbsId=BOARD00019"),

    # 한국교통연구원 (KOTI)
    ("koti/report", "https://www.koti.re.kr/user/bbs/bassRsrchReprtList.do"),

    # 한국환경연구원 (KEI)
    ("kei/report", "https://www.kei.re.kr/elibList.es?mid=a10101010000"),
    ("kei/forum", "https://www.kei.re.kr/elibList.es?mid=a10102010000&elibName=environmentalforum"),
    ("kei/brief1", "https://www.kei.re.kr/board.es?mid=a10102020000&bid=0028"),
    ("kei/brief2", "https://www.kei.re.kr/board.es?mid=a10102060000&bid=0032"),
    ("kei/brief3", "https://www.kei.re.kr/board.es?mid=a10102100000&bid=0054"),
    ("kei/brief4", "https://www.kei.re.kr/board.es?mid=a10102120000&bid=0057"),
    ("kei/brief5", "https://www.kei.re.kr/board.es?mid=a10102050000&bid=0031"),
    ("kei/brief6", "https://www.kei.re.kr/board.es?mid=a10102180000&bid=0081"),
    ("kei/brief7", "https://www.kei.re.kr/board.es?mid=a10102130000&bid=0075"),
    ("kei/brief8", "https://www.kei.re.kr/board.es?mid=a10102070200&bid=0033"),
    ("kei/press", "https://www.kei.re.kr/board.es?mid=a10307020000&bid=0009"),

    # 한국교육개발원 (KEDI)
    ("kedi/report", "https://www.kedi.re.kr/khome/main/research/listPubForm.do"),
    ("kedi/brief", "https://www.kedi.re.kr/khome/main/research/kediBrief.do"),
    ("kedi/journal", "https://www.kedi.re.kr/khome/main/journal/listEDJournalForm.do"),
    ("kedi/press", "https://www.kedi.re.kr/khome/main/announce/listBroadAnnounceForm.do"),

    # 한국농촌경제연구원 (KREI)
    ("krei/report", "https://www.krei.re.kr/krei/page/53"),
    ("krei/press", "https://www.krei.re.kr/krei/page/24"),

    # 국토연구원 (KRIHS)
    ("krihs/report", "https://www.krihs.re.kr/krihsLibraryReport/reportList.es?mid=a10102000000"),
    ("krihs/journal1", "https://www.krihs.re.kr/krihsLibraryArticle/articleList.es?mid=a10103010000&pub_kind=1"),
    ("krihs/journal2", "https://www.krihs.re.kr/krihsLibraryArticle/articleList.es?mid=a10103020000&pub_kind=2"),
    ("krihs/journal3", "https://www.krihs.re.kr/krihsLibraryArticle/articleList.es?mid=a10103030000&pub_kind=3"),
    ("krihs/journal4", "https://www.krihs.re.kr/krihsLibraryArticle/articleList.es?mid=a10103040000&pub_kind=6"),
    ("krihs/brief1", "https://www.krihs.re.kr/krihsLibraryReport/briefList.es?mid=a10103050000&pub_kind=BR_1"),
    ("krihs/brief2", "https://www.krihs.re.kr/krihsLibraryReport/briefList.es?mid=a10103060000&pub_kind=BR_2"),
    ("krihs/journal5", "https://www.krihs.re.kr/krihsLibraryArticle/articleList.es?mid=a10103070000&pub_kind=7"),
    ("krihs/journal6", "https://www.krihs.re.kr/krihsLibraryArticle/articleList.es?mid=a10103080000&pub_kind=9"),
    ("krihs/working", "https://www.krihs.re.kr/krihsLibraryReport/briefList.es?mid=a10103090000&pub_kind=WKP"),
    ("krihs/press", "https://www.krihs.re.kr/board.es?mid=a10607000000&bid=0008"),

    # 건축공간연구원 (AURI)
    ("auri/report", "https://www.auri.re.kr/publication/list.es?mid=a10312000000&publication_type=research"),
    ("auri/press", "https://www.auri.re.kr/board.es?mid=a10401030000&bid=0013"),

    # 과학기술정책연구원 (STEPI)
    ("stepi/report1", "https://www.stepi.re.kr/site/stepiko/report/List.do?cateCont=A0201"),
    ("stepi/report2", "https://www.stepi.re.kr/site/stepiko/report/List.do?cateCont=A0203"),
    ("stepi/report3", "https://www.stepi.re.kr/site/stepiko/report/List.do?cateCont=A0202"),
    ("stepi/report4", "https://www.stepi.re.kr/site/stepiko/report/List.do?cateCont=A0204"),
    ("stepi/report5", "https://www.stepi.re.kr/site/stepiko/report/List.do?cateCont=A0205"),
    ("stepi/report6", "https://www.stepi.re.kr/site/stepiko/report/List.do?cateCont=A0206"),
    ("stepi/issue", "https://www.stepi.re.kr/site/stepiko/ex/bbs/reportCateList.do?cateTypeCd=A05"),
    ("stepi/press", "https://www.stepi.re.kr/site/stepiko/ex/bbs/List.do?cbIdx=1205"),

    # 한국철도공사 (KORAIL)
    ("korail/press", "https://info.korail.com/info/selectBbsNttList.do?bbsNo=199&key=911"),

    # 한국고용정보원 (KEIS)
    ("keis/pub", "https://www.keis.or.kr/keis/ko/proj/113/pblc/list.do"),
    ("keis/press", "https://www.keis.or.kr/keis/ko/bbs/123/list.do?searchCl1=1"),
    ("keis/data", "https://www.keis.or.kr/keis/ko/bbs/124/list.do?searchCl1=1"),

    # 한국전력공사 (KEPCO)
    ("kepco/press", "https://www.kepco.co.kr/home/media/newsroom/pr/boardList.do"),

    # 서울시 (Seoul)
    ("seoul/research", "https://opengov.seoul.go.kr/research/list"),
    ("seoul/press", "https://opengov.seoul.go.kr/press/list"),
    ("seoul/data", "https://opengov.seoul.go.kr/data/list"),

    # 부산시 (Busan)
    ("busan/press", "https://www.busan.go.kr/nbtnewsBU"),
    ("busan/data", "https://data.busan.go.kr/bdip/opendata/dataSet.do;jsessionid=6B10E30C8AD71286859FAAF148A85DF8.worker2"),

    # 대구시 (Daegu)
    ("daegu/press", "https://info.daegu.go.kr/newshome/mtnmain.php?mtnkey=scatelist&mkey=26"),

    # 인천시 (Incheon)
    ("incheon/press", "https://www.incheon.go.kr/IC010205"),

    # 광주시 (Gwangju)
    ("gwangju/press", "https://www.gwangju.go.kr/boardList.do?boardId=BD_0000000027&pageId=www789"),

    # 대전시 (Daejeon)
    ("daejeon/press", "https://www.daejeon.go.kr/drh/board/boardNormalList.do?boardId=normal_0189&menuSeq=6825"),
    ("daejeon/stats", "https://www.daejeon.go.kr/sta/StaStatisticsFldList.do?menuSeq=180"),

    # 울산시 (Ulsan)
    ("ulsan/press", "https://www.ulsan.go.kr/u/rep/bbs/list.ulsan?bbsId=BBS_0000000000000170&mId=001003007001000000"),
    ("ulsan/data", "https://www.ulsan.go.kr/u/rep/bbs/list.ulsan?bbsId=BBS_0000000000000027&mId=001004003001000000"),

    # 세종시 (Sejong)
    ("sejong/press", "https://www.sejong.go.kr/bbs/R0079/list.do"),

    # 경기도 (Gyeonggi)
    ("gyeonggi/press", "https://gnews.gg.go.kr/briefing/brief_gongbo.do"),

    # 충남 (Chungnam)
    ("chungnam/press", "https://www.chungnam.go.kr/cnportal/cnapcPressList/cnapcPress/list.do?menuNo=500498"),

    # 전남 (Jeonnam)
    ("jeonnam/press", "https://www.jeonnam.go.kr/M7116/boardList.do?menuId=jeonnam0202000000"),

    # 경북 (Gyeongbuk)
    ("gb/data", "https://gb.go.kr/Sub/open_contents/section/datastat/page.do?mnu_uid=7866&LARGE_CODE=820&MEDIUM_CODE=20&SMALL_CODE=40&SMALL_CODE2=20&mnu_order=3"),
    ("gb/press", "https://gb.go.kr/Main/page.do?mnu_uid=6792&LARGE_CODE=720&MEDIUM_CODE=50&SMALL_CODE=10&SMALL_CODE2=60&"),

    # 경남 (Gyeongnam)
    ("gyeongnam/press", "https://www.gyeongnam.go.kr/board/list.gyeong?boardId=BBS_0000060&menuCd=DOM_000000135002001000&contentsSid=6959&cpath="),

    # 강원 (Gangwon)
    ("gangwon/press", "https://state.gwd.go.kr/portal/briefing/pressRelease/newpressRelease"),

    # 제주도 (Jeju)
    ("jeju/press", "https://www.jeju.go.kr/news/bodo/list.htm"),

    # 방송통신위원회 (KCC)
    ("kcc/press", "https://www.kcc.go.kr/user.do?boardId=1113&page=A05030000&dc=K05030000"),

    # 한국통신사업자연합회 (KMCC)
    ("kmcc/data", "https://www.kmcc.go.kr/user.do?boardId=1030&page=A02060400&dc=K02060400"),

    # 국가인권위원회 (NHRCK)
    ("nhrck/press", "https://www.humanrights.go.kr/base/board/list?boardManagementNo=24&menuLevel=3&menuNo=91"),
    ("nhrck/data", "https://www.humanrights.go.kr/base/board/list?boardManagementNo=20&menuLevel=3&menuNo=120"),

    # 선거관리위원회 (NEC)
    ("nec/press", "https://www.nec.go.kr/site/nec/ex/bbs/List.do?cbIdx=1090"),
    ("nec/data", "https://www.nec.go.kr/site/nec/ex/bbs/List.do?cbIdx=1132"),

    # 감사원 (BAI)
    ("bai/data", "https://www.bai.go.kr/bai/board/base/list?brdId=BAK_0007"),

    # 대법원 (Supreme Court)
    ("scourt/press", "https://www.scourt.go.kr/portal/news/NewsListAction.work?gubun=6"),

    # 법제처 (MOLEG)
    ("moleg/press", "https://www.moleg.go.kr/board.es?mid=a10501000000&bid=0048"),
    ("moleg/data", "https://www.moleg.go.kr/board.es?mid=a10404000000&bid=0007"),

    # 서울연구원 (SI)
    ("si/report", "https://www.si.re.kr/bbs/list.do?pstSn=&key=2024100039&sc_detailAt=&subject=0&pageIndex=1&orderBy=bbsOrdr+desc&sc_subJectId=SUBJECT_CD&sw=&sc_writer=&sc_pubDateS=&sc_pubDateE=&sc_dataSeCd99="),
    ("si/data", "https://www.si.re.kr/bbs/list.do?key=2024100166"),
    ("si/press", "https://www.si.re.kr/bbs/list.do?key=2024100192"),

    # 경기연구원 (GRI)
    ("gri/report", "https://www.gri.re.kr/web/contents/resreport.do"),
    ("gri/press", "https://www.gri.re.kr/web/contents/media01.do"),

    # 인천연구원 (II)
    ("ii/report", "https://www.ii.re.kr/base/board/list?boardManagementNo=14&menuLevel=2&menuNo=76"),
    ("ii/press", "https://www.ii.re.kr/base/board/list?boardManagementNo=17&menuLevel=3&menuNo=89"),

    # 광주전남연구원 (GI)
    ("gi/report", "https://www.gi.re.kr/Home/H10000/H10100/pmsReportList"),
    ("gi/press", "https://www.gi.re.kr/Home/H10000/H10200/boardList"),
    ("gi/data", "https://www.gi.re.kr/Home/H30000/H30400/H30401/boardList"),
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def is_success(status, size):
    if not status or status >= 400:
        return False
    if size < 500:
        return False
    return True


def test_requests_method(url, timeout=60):
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    start = time.time()
    try:
        resp = session.get(url, timeout=timeout)
        elapsed = time.time() - start
        content = resp.text[:500].lower()
        has_captcha = "captcha" in content or "challenge" in content
        return {
            "method": "requests",
            "status": resp.status_code,
            "size": len(resp.content),
            "time": round(elapsed, 2),
            "captcha": has_captcha,
            "error": None,
        }
    except Exception as e:
        return {
            "method": "requests",
            "status": None,
            "size": 0,
            "time": round(time.time() - start, 2),
            "captcha": False,
            "error": str(e)[:120],
        }


def test_cloudscraper_method(url, timeout=60):
    scraper = cloudscraper.create_scraper()
    start = time.time()
    try:
        resp = scraper.get(url, timeout=timeout)
        elapsed = time.time() - start
        content = resp.text[:500].lower()
        has_captcha = "captcha" in content or "challenge" in content
        return {
            "method": "cloudscraper",
            "status": resp.status_code,
            "size": len(resp.content),
            "time": round(elapsed, 2),
            "captcha": has_captcha,
            "error": None,
        }
    except Exception as e:
        return {
            "method": "cloudscraper",
            "status": None,
            "size": 0,
            "time": round(time.time() - start, 2),
            "captcha": False,
            "error": str(e)[:120],
        }


def test_browser_method(url, browser, timeout=60):
    start = time.time()
    page = browser.new_page(user_agent=USER_AGENT)
    try:
        resp = page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
        status = resp.status if resp else None
        content = page.content()
        elapsed = time.time() - start
        has_captcha = "captcha" in content[:500].lower() or "challenge" in content[:500].lower()
        return {
            "method": "browser",
            "status": status,
            "size": len(content),
            "time": round(elapsed, 2),
            "captcha": has_captcha,
            "error": None,
        }
    except Exception as e:
        return {
            "method": "browser",
            "status": None,
            "size": 0,
            "time": round(time.time() - start, 2),
            "captcha": False,
            "error": str(e)[:120],
        }
    finally:
        page.close()


def main():
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"Testing {len(KR_URLS)} Korean URLs with {timeout}s timeout...\n")

    results = []
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)

    try:
        for name, url in KR_URLS:
            print(f"\n--- [{name}] {url[:80]}{'...' if len(url)>80 else ''} ---")
            row = {"name": name, "url": url, "results": [], "best_method": None}

            for test_fn in [test_requests_method, test_cloudscraper_method]:
                r = test_fn(url, timeout)
                row["results"].append(r)
                ok = is_success(r["status"], r["size"]) and not r["captcha"]
                icon = "OK" if ok else "FAIL"
                print(f"  {r['method']:15s} {icon:4s}  status={r['status']}  size={r['size']:>8,}  time={r['time']}s"
                      + (f"  err={r['error'][:60]}" if r["error"] else "")
                      + ("  [CAPTCHA]" if r["captcha"] else ""))
                if ok:
                    row["best_method"] = r["method"]
                    break
            else:
                r = test_browser_method(url, browser, timeout)
                row["results"].append(r)
                ok = is_success(r["status"], r["size"]) and not r["captcha"]
                icon = "OK" if ok else "FAIL"
                print(f"  {r['method']:15s} {icon:4s}  status={r['status']}  size={r['size']:>8,}  time={r['time']}s"
                      + (f"  err={r['error'][:60]}" if r["error"] else "")
                      + ("  [CAPTCHA]" if r["captcha"] else ""))
                if ok:
                    row["best_method"] = r["method"]

            results.append(row)
    finally:
        browser.close()
        pw.stop()

    # Save report
    report = {
        "timestamp": datetime.now().isoformat(),
        "timeout": timeout,
        "total": len(results),
        "results": results,
    }
    report_dir = Path(__file__).resolve().parent.parent / "reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"kr_test_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport saved: {report_path}")

    # Summary
    methods = {"requests": [], "cloudscraper": [], "browser": [], None: []}
    for r in results:
        methods[r["best_method"]].append(r["name"])

    print(f"\n{'='*60}")
    print(f"SUMMARY ({len(results)} URLs tested, timeout={timeout}s)")
    print(f"{'='*60}")
    for cat, label in [("requests", "A (requests)"), ("cloudscraper", "B (cloudscraper)"), ("browser", "C (browser)"), (None, "D (FAILED)")]:
        print(f"  Category {label}: {len(methods[cat])}")
        for n in methods[cat]:
            print(f"    - {n}")


if __name__ == "__main__":
    main()

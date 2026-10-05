# 재실·활용률 추정 (occupancy-estimate)

## 한 줄 요약
CO2 증가율, 적외선 활동, 소음, 조도로 재실 여부와 활용률을 추정합니다.

## 언제 쓰나
- 이 공간은 실제로 얼마나 쓰이나?
- 이 공간은 언제 붐비고 언제 비나?
- 예약했지만 안 쓴 시간은?
- 어느 공간을 줄여도 되나?

## 언제 쓰면 안 되나
- 재실 신호가 1개뿐일 때(2개 이상 필요)
- 정확한 인원 수가 필요할 때(인원 수는 내지 않습니다)

## 필요한 데이터
- 역할 co2(의미 태그 co2), activity(activity), noise(noise), illuminance(illuminance) 중 2개 이상
- 최소 기간: 7일
- 최소 포인트: 500개
- 누락률 경고: 10%
- 누락률 실패: 30%
- 권장 집계 단위: 1m

## 파라미터
- `operatingStart`: 운영 시작 시각(HH:MM, 조직 시간대, 기본 09:00). 활용률의 분모가 되는 운영 시간의 시작입니다.
- `operatingEnd`: 운영 끝 시각(기본 18:00).
- `weekdaysOnly`: true(기본)면 평일만 운영 시간으로 셉니다.
- `minOccupiedMinutes`: 이보다 짧은 재실 판정은 버립니다(분, 기본 5). 늘리면 잠깐 지나간 사람을 덜 셉니다.

## 결과 읽는 법
재실 타임라인은 사용 중으로 추정한 구간이고, 히트맵은 요일 × 시간대별 사용 비율(%)입니다. 활용률은 운영 시간 중 사용 중으로 추정한 시간의 비율입니다. 모든 값은 추정치이며 인원 수는 표시하지 않습니다.

## 주의점
- 재실 여부와 활용률은 센서로 본 추정치입니다.
- 실제 재실 기록(예약 시스템 등)이 있으면 연결해서 확인하고 보정하세요.
- 인원 표시는 NFR-13.02를 따라 어디에도 정확한 숫자를 내지 않습니다.

## 사용 예시
- 회의실 CO2와 적외선 14일로 운영 시간 활용률이 35%라는 것을 보고 공간 재배치를 검토합니다.
- 소음과 조도만 있는 휴게실의 요일별 사용 패턴을 봅니다.

## 알고리즘
신호별 근거를 0/1로 만들고(활동: 5분 안 감지 2회 이상, CO2: 10분 증가율 분당 3ppm 초과 또는 하위 5% 대비 250ppm 이상, 소음: 하위 10% 대비 8dB 이상, 조도: 하위 10% 대비 100lux 이상) 가중 평균이 0.5 이상이면 재실로 봅니다. 5분 이하 끊김은 메우고 minOccupiedMinutes보다 짧은 재실은 버립니다.

## 참고 문헌
- Dong et al., An information technology enabled sustainability test-bed for occupancy detection, Energy and Buildings 2010
- Candanedo, Feldheim, Accurate occupancy detection of an office room from light, temperature, humidity and CO2, 2016

import re
import pyLuogu
import openai
import asyncio
import asynciolimiter
import os
import httpx
import pytesseract
from PIL import Image

import pyLuogu.errors

pyLuogu.set_log_level("WARNING")

cookies_openai_agent = pyLuogu.LuoguCookies.from_file("cookies_openai_agent.json")
luogu_openai_agent = pyLuogu.asyncLuoguAPI(cookies=cookies_openai_agent)

LACPT_id = 702688
base_url = "https://api.openai-hk.com/v1"
api_key = open("openai-hk_key", "r").read()
model = "o1-pro-all"
reasoning_effort = None
max_tokens = 32767
prompt = "请仅给出该题目的完整，正确的 C++ 实现，而无需输出任何其他的内容，将代码使用 markdown 多行代码框格式格式化。"
extra_body = {
    "provider" : {
        "order": ["Azure"]
    }
}
maximal_parallel = 25
skipped = []

openai_client = openai.AsyncOpenAI(
    base_url=base_url,
    api_key=api_key,
    max_retries=10,
    timeout=httpx.Timeout(100.0)
)

rate_limiter_fetch = asynciolimiter.Limiter(0.4)
rate_limiter_submit = asynciolimiter.Limiter(0.04)
rate_limiter_openai = asynciolimiter.Limiter(1000)
sem = asyncio.Semaphore(maximal_parallel)

def manual_captcha_handler(data: bytes):
    with open(".temp/captcha.jpg", "wb") as f:
        f.write(data)
    os.system("imgcat .temp/captcha.jpg")
    return input("captcha: ")

def captcha_handler(data: bytes, attempt: int):
    with open(".temp/captcha.jpg", "wb") as f:
        f.write(data)
    captcha_text = pytesseract.image_to_string(
        Image.open(".temp/captcha.jpg"), 
        config='--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789').strip()
    print(f"captcha: {captcha_text}")
    if attempt >= 3:
        return manual_captcha_handler(data)
    return captcha_text

async def test_model_inner(pid: int, problem_content: str, pass_num: int = 1):
    f = open(f".temp/{pid}.log", "w")
    stream = await openai_client.chat.completions.create(
        model=model,
        messages=[
            { "role": "user", "content": problem_content + "\n" + prompt },
        ],
        reasoning_effort=reasoning_effort,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=max_tokens,
        temperature=0.0,
        extra_body=extra_body
    )

    now_time = asyncio.get_event_loop().time()
    start_time = now_time
    last_time = now_time
    answer = ""
    reasoning_text = ""
    token_count = 0
    chunk_id = 0

    chunk_header = f"[{int(start_time - now_time):04}#00000]"
    print(f"{chunk_header} {model} started on running {pid:<6}.")
    f.write(f"{chunk_header} {model} started on running {pid:<6}.\n")
    f.flush()

    try:
        async for chunk in stream:
            chunk_id += 1
            chunk_header = f"[{int(now_time - start_time):04}#{chunk_id:05}]"
            await rate_limiter_openai.wait()
            now_time = asyncio.get_event_loop().time()
            try:
                delta = chunk.choices[0].delta
                usage = chunk.usage
                f.write(f"{chunk_header} {str(delta)} \n")
            except IndexError:
                f.write(f"{chunk_header} {str(chunk)} \n")
                print(f"{chunk_header} {pid} delta is None")
                continue
            if delta is None:
                f.write(f"{chunk_header} {str(chunk)} \n")
                print(f"{chunk_header} {pid} delta is None")
                continue
            f.flush()
            
            additional_answer = delta.content or ""
            additional_reasonig_text = ""
            if hasattr(delta, 'reasoning_content') and delta.reasoning_content != None:
                additional_reasonig_text = delta.reasoning_content
            if hasattr(delta, 'reasoning') and delta.reasoning != None:
                additional_reasonig_text = delta.reasoning
            answer += additional_answer
            reasoning_text += additional_reasonig_text

            time_delta = now_time - last_time
            if usage is not None:
                token_count = usage.completion_tokens
                tokens_per_second = token_count / (now_time - start_time)
                print(f"{chunk_header} {model} is running on {pid:<6}. [{token_count:5}(+ {token_count:3}) tokens, {tokens_per_second:5.2f} tps]")
            else:
                word_count = len(answer) + len(reasoning_text)
                word_delta = len(additional_answer) + len(additional_reasonig_text)
                word_per_second = word_delta / time_delta
                print(f"{chunk_header} {model} is running on {pid:<6}. [{word_count:5}(+ {word_delta:3}) chars , {word_per_second:5.2f} wps]")
            last_time = now_time
    except Exception as e:
        f.write(f"{chunk_header} {e}")
        print(f"{chunk_header} {pid} raised {e}")

        f.close()
        raise e
    
    now_time = asyncio.get_event_loop().time()
    f.write(f"{chunk_header} Done with\n {answer}")
    f.close()
    print(f"{chunk_header} {pid} done.")

    return answer, int(now_time - start_time)

log = open(".temp/log.log", "w")

async def test_model(pid: int, pass_num: int = 1):    
    if pass_num > 1:
        raise NotImplementedError("pass_num > 1 is not supported.")
    
    if pid in skipped:
        return "Skipped", "N/A"
    
    await rate_limiter_fetch.wait()
    problem = (await luogu_openai_agent.get_problem(pid)).problem

    max_retry = 20
    for attemp in range(max_retry):
        try:
            async with sem:
                log.write(f"Testing {pid}({attemp + 1}/{max_retry})...\n")
                log.flush()
                answer, used_time = await test_model_inner(pid, problem.content.get_markdown())
            if answer == "":
                log.write(f"{pid} got empty output.\n")
                log.flush()
                continue
        except Exception as e:
            print(f"{pid} raised {e}")
            log.write(f"{pid} raised {e}\n")
            log.flush()
            await asyncio.sleep(100)
            continue
        break
    else:
        return "Failed (nothing returned)", "N/A"
    
    realcode = re.search(r"```(cpp)?\n([\S\s]*)\n```", answer)
    if not realcode:
        print(f"{pid} got no code.")
        log.write(f"{pid} got no code.\n")
        log.flush()
        return "Failed (no code)", "N/A"
    answer = realcode.group(2)

    log.write(f"{pid} got code after {used_time}\n")
    log.flush()

    max_retry = 5
    for attemp in range(max_retry):
        try:
            await rate_limiter_submit.wait()
            log.write(f"Submitting {pid}({attemp + 1}/{max_retry})...\n")
            log.flush()
            rid = (await luogu_openai_agent.submit_code(
                pid, 
                answer, 
                capture_handler=captcha_handler)
            ).rid
            break
        except pyLuogu.errors.ForbiddenError:    
            await asyncio.sleep(20)
            continue
        except Exception as e:
            return f"Failed({e})", "N/A"
    else:
        return "Failed(Forbidden)", "N/A"

    max_retry = 25
    for attemp in range(max_retry):
        await rate_limiter_fetch.wait()
        try:
            res = await luogu_openai_agent.get_record(rid)
        except Exception as e:
            continue
        if res.record.status in [0, 1]:
            await asyncio.sleep(5)
            continue

        if res.record.status == 2:
            log.write(f"{pid} got CE after {used_time}\n")
            log.flush()
            return "CE", str(used_time)
        
        if res.record.score is None:
            res.record.score = 100 if res.record.status == 12 else 0
        
        log.write(f"{pid} got {res.record.score} after {used_time}\n")
        log.flush()
        return str(res.record.score), str(used_time)
    else:
        return "Failed(Infinite judging)", "N/A"

async def main():
    problems = (await luogu_openai_agent.get_problem_set(LACPT_id)).training.problems
    print("Problem set loaded.")

    results = await asyncio.gather(*[test_model(problem.pid) for problem in problems])
    
    with open(f".temp/{model.replace("/","_")}.csv", "w") as f:
        for result in results:
            print("\t".join(result))
            f.write(",".join(result) + "\n")

if __name__ == "__main__":
    print(f"Testing model {model} on LACPT")
    asyncio.run(main())

import "server-only";

export const operatorHeaders = (json=false): Record<string,string> => {
  const token=process.env.DELIVERY_API_TOKEN;
  return {...(json?{"content-type":"application/json"}:{}),...(token?{"x-delivery-token":token}:{})};
};
